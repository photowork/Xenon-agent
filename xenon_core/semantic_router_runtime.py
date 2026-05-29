from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from xenon_core.model_request import build_chat_completion_kwargs


def build_semantic_router_catalog(
    *,
    tool_schemas: List[Dict[str, Any]],
    module_names: List[str],
) -> str:
    lines = []
    for module_name in module_names:
        tool_names = []
        for schema in tool_schemas:
            tool_name = (schema.get("function", {}) or {}).get("name", "")
            if tool_name.startswith(module_name + "_"):
                tool_names.append(tool_name)
        preview = ", ".join(tool_names[:4]) if tool_names else "no tools"
        lines.append(f"- {module_name}: {preview}")
    return "\n".join(lines)


def parse_semantic_route_response(content: str) -> Optional[Dict[str, Any]]:
    text = str(content or "").strip()
    if not text:
        return None

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or start >= end:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None


def infer_semantic_route(
    *,
    user_input: str,
    tool_schemas: List[Dict[str, Any]],
    current_task: Optional[Dict[str, Any]],
    get_module_list_fn: Callable[[], List[str]],
    routing_client: Any,
    router_model: str,
    router_max_tokens: int,
    logger: Any,
    router_thinking_enabled: Optional[bool] = False,
    router_reasoning_effort: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not user_input.strip():
        return None

    module_catalog = build_semantic_router_catalog(
        tool_schemas=tool_schemas,
        module_names=get_module_list_fn(),
    )
    if not module_catalog:
        return None

    execution_state = (current_task or {}).get("execution_state", {}) if current_task else {}
    current_phase = execution_state.get("phase", "")
    system_prompt = (
        "You are a routing model for a coding agent.\n"
        "Return JSON only with keys: phase, intent, candidate_modules, candidate_tools, confidence, reasoning.\n"
        "Valid phases: analyze, locate, edit, test, debug, deploy, reflect, maintenance.\n"
        "Valid intents: general_execution, fix_and_verify, integrate_change, analyze_codebase, remote_operation.\n"
        "Prefer code_editor_handler for editing files, code_navigator for reading code, "
        "terminal_handler for running commands, debug_handler for debugging, file_manager for filesystem work.\n"
        "Do not invent modules or tools outside the provided catalog."
    )
    user_prompt = (
        f"User request:\n{user_input}\n\n"
        f"Current task phase: {current_phase or 'none'}\n\n"
        f"Available modules and sample tools:\n{module_catalog}\n\n"
        "Return compact JSON."
    )

    try:
        request_kwargs = build_chat_completion_kwargs(
            model=router_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=router_max_tokens,
            temperature=0.1,
            thinking_enabled=router_thinking_enabled,
            reasoning_effort=router_reasoning_effort,
        )
        response = routing_client.chat.completions.create(**request_kwargs)
        content = response.choices[0].message.content or ""
        parsed = parse_semantic_route_response(content)
        if parsed:
            return parsed
        logger.warning("Semantic router returned unparsable content: %s", content[:200])
        return None
    except Exception as error:
        logger.warning("Semantic routing failed, falling back to rules: %s", error)
        return None
