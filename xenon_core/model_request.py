from __future__ import annotations

from typing import Any, Dict, List, Optional


THINKING_ENABLED = "enabled"
THINKING_DISABLED = "disabled"


def build_chat_completion_kwargs(
    *,
    model: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    stream: Optional[bool] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    thinking_enabled: Optional[bool] = None,
    reasoning_effort: Optional[str] = None,
    extra_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build OpenAI-compatible request kwargs for DeepSeek's thinking controls."""
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
    }

    if tools is not None:
        kwargs["tools"] = tools
    if stream is not None:
        kwargs["stream"] = stream
        if stream:
            kwargs["stream_options"] = {"include_usage": True}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if temperature is not None:
        kwargs["temperature"] = temperature

    request_extra_body = dict(extra_body or {})
    if thinking_enabled is not None:
        thinking_payload = dict(request_extra_body.get("thinking") or {})
        thinking_payload["type"] = THINKING_ENABLED if thinking_enabled else THINKING_DISABLED
        request_extra_body["thinking"] = thinking_payload

    if request_extra_body:
        kwargs["extra_body"] = request_extra_body

    if thinking_enabled and reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort

    return kwargs
