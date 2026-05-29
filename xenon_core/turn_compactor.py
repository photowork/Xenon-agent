from __future__ import annotations

import ast
import copy
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple


_DROP_MESSAGE_KEYS = {
    "debug",
    "raw_trace",
    "stderr",
    "stdout",
    "file_content",
    "full_content",
    "raw_format",
    "schema",
    "tool_schema",
    "tool_schemas",
    "tools",
}

_TOO_SHORT_FINALS = {
    "ok",
    "done",
    "搞定了",
    "好了",
    "完成了",
    "已完成",
    "已搞定",
    "成功了",
}

_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s'\"<>|]+(?:[\\/][^\s'\"<>|]+)*|/[^\s'\"<>|]+(?:/[^\s'\"<>|]+)*)"
)


def compact_turn_for_next_context(turn_messages: list[dict]) -> list[dict]:
    """
    输入：当前轮完整 live_messages
    输出：下一轮允许携带的极简消息，只保留用户提问和最终成果回复
    """
    user_content, last_user_index = _find_last_message_content(turn_messages, "user")
    assistant_content, _ = _find_last_assistant_content(turn_messages)

    if _assistant_content_too_short(assistant_content):
        assistant_content = _build_outcome_summary(turn_messages, start_index=last_user_index) or assistant_content

    compact_messages: List[Dict[str, str]] = []
    if user_content:
        compact_messages.append({"role": "user", "content": user_content})
    if assistant_content:
        compact_messages.append({"role": "assistant", "content": assistant_content})
    return compact_messages


def sanitize_messages_for_api(
    messages: list[dict],
    *,
    preserve_current_toolchain: bool = False,
    include_reasoning: bool = False,
) -> list[dict]:
    """Return API-safe messages without historical raw traces or debug payloads.

    By default this is strict and removes tool-role messages, assistant
    ``tool_calls`` and ``reasoning_content``. During an active tool chain the
    caller may opt into preserving tool calls/results for the current live turn.
    """
    sanitized: List[Dict[str, Any]] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue

        role = message.get("role")
        if role == "tool" and not preserve_current_toolchain:
            continue
        if role not in {"system", "user", "assistant", "tool"}:
            continue

        cleaned: Dict[str, Any] = {}
        for key, value in message.items():
            if key in _DROP_MESSAGE_KEYS:
                continue
            if key == "reasoning_content":
                if include_reasoning and preserve_current_toolchain and message.get("tool_calls"):
                    cleaned[key] = copy.deepcopy(value)
                continue
            if key == "tool_calls":
                if preserve_current_toolchain:
                    cleaned[key] = copy.deepcopy(value)
                continue
            if key == "content":
                cleaned[key] = _sanitize_content_value(value)
                continue
            cleaned[key] = copy.deepcopy(value)

        cleaned["role"] = role
        if role == "tool":
            if not cleaned.get("tool_call_id"):
                continue
            cleaned.setdefault("content", "")
        if role == "assistant" and not cleaned.get("content") and not cleaned.get("tool_calls"):
            continue
        sanitized.append(cleaned)

    return sanitized


def trim_compact_history(messages: list[dict], max_turns: int) -> list[dict]:
    if max_turns <= 0:
        return []

    state_messages = [
        copy.deepcopy(message)
        for message in messages
        if message.get("role") == "system"
        and str(message.get("content", "")).startswith("【任务状态检查点】")
    ]
    compact_messages = [
        copy.deepcopy(message)
        for message in messages
        if message.get("role") in {"user", "assistant"}
    ]
    return state_messages + compact_messages[-max_turns * 2 :]


def _find_last_message_content(messages: list[dict], role: str) -> Tuple[str, int]:
    for index in range(len(messages or []) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != role:
            continue
        content = _content_to_text(message.get("content", "")).strip()
        if content:
            return content, index
    return "", -1


def _find_last_assistant_content(messages: list[dict]) -> Tuple[str, int]:
    for index in range(len(messages or []) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "assistant":
            continue
        content = _content_to_text(message.get("content", "")).strip()
        if content:
            return content, index
    return "", -1


def _assistant_content_too_short(content: str) -> bool:
    normalized = _shorten_one_line(content, max_chars=40).lower()
    return not normalized or len(normalized) < 8 or normalized in _TOO_SHORT_FINALS


def _build_outcome_summary(messages: list[dict], *, start_index: int) -> str:
    scoped_messages = messages[start_index + 1 :] if start_index >= 0 else messages
    successes: List[bool] = []
    paths: List[str] = []
    statuses: List[str] = []
    errors: List[str] = []

    for message in scoped_messages:
        role = message.get("role")
        if role not in {"tool", "assistant"}:
            continue
        payload = _parse_payload(message.get("content", ""))
        success, payload_paths, payload_statuses, payload_errors = _extract_outcome_bits(payload)
        if success is not None:
            successes.append(success)
        paths.extend(payload_paths)
        statuses.extend(payload_statuses)
        errors.extend(payload_errors)

    parts: List[str] = []
    if successes:
        parts.append("操作成功" if successes[-1] else "操作失败")
    elif paths or statuses:
        parts.append("已完成本轮操作")
    elif errors:
        parts.append("本轮操作遇到错误")

    unique_paths = _dedupe(paths)
    if unique_paths:
        parts.append("关键文件: " + ", ".join(unique_paths[:3]))

    unique_statuses = _dedupe(statuses)
    if unique_statuses:
        parts.append("状态: " + "；".join(unique_statuses[:2]))

    unique_errors = _dedupe(errors)
    if unique_errors:
        parts.append("错误: " + unique_errors[0])

    if not parts:
        return ""
    return "；".join(parts) + "。"


def _parse_payload(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    text = _content_to_text(value).strip()
    if not text:
        return ""

    if text[0] in "{[":
        for parser in (json.loads, ast.literal_eval):
            try:
                return parser(text)
            except Exception:
                pass
    return text


def _extract_outcome_bits(payload: Any) -> Tuple[Optional[bool], List[str], List[str], List[str]]:
    successes: List[bool] = []
    paths: List[str] = []
    statuses: List[str] = []
    errors: List[str] = []

    def walk(value: Any, key_hint: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = str(key).lower()
                if lowered == "success" and isinstance(item, bool):
                    successes.append(item)
                elif lowered in {"path", "filepath", "file_path", "output_path", "saved_path", "target_path"}:
                    paths.extend(_extract_paths(item))
                elif lowered in {"artifact", "artifacts", "files", "file", "output_file"}:
                    paths.extend(_extract_paths(item))
                elif lowered in {"error", "exception", "failure"} and item:
                    errors.append(_shorten_one_line(item, max_chars=180))
                elif lowered in {"message", "status", "state", "summary"} and item:
                    statuses.append(_shorten_one_line(item, max_chars=160))
                if lowered not in {"stdout", "stderr", "raw_trace", "file_content", "raw_format", "schema"}:
                    walk(item, lowered)
            return

        if isinstance(value, list):
            for item in value:
                walk(item, key_hint)
            return

        text = _content_to_text(value)
        paths.extend(_extract_paths(text))
        if key_hint in {"error", "exception", "failure"} and text:
            errors.append(_shorten_one_line(text, max_chars=180))

    walk(payload)
    success = successes[-1] if successes else None
    return success, paths, statuses, errors


def _extract_paths(value: Any) -> List[str]:
    if isinstance(value, (list, tuple, set)):
        paths: List[str] = []
        for item in value:
            paths.extend(_extract_paths(item))
        return paths
    if isinstance(value, dict):
        paths: List[str] = []
        for item in value.values():
            paths.extend(_extract_paths(item))
        return paths
    text = _content_to_text(value)
    return [_strip_path_punctuation(match.group(0)) for match in _PATH_RE.finditer(text)]


def _strip_path_punctuation(path: str) -> str:
    return path.rstrip(".,;:)]}")


def _sanitize_content_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_content_value(item)
            for key, item in value.items()
            if str(key) not in _DROP_MESSAGE_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_content_value(item) for item in value]
    return copy.deepcopy(value)


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_content_to_text(item) for item in content)
    if isinstance(content, dict):
        if "text" in content:
            return _content_to_text(content.get("text"))
        return json.dumps(content, ensure_ascii=False, default=str)
    return str(content)


def _shorten_one_line(value: Any, *, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", _content_to_text(value)).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _dedupe(values: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        item = _shorten_one_line(value, max_chars=220)
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
