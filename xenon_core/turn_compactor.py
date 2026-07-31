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

_TOOL_EVIDENCE_USER_RE = re.compile(
    r"(?i)(?:"
    r"[A-Za-z]:[\\/]|/[^\s'\"<>|]+|"
    r"\.(?:py|js|ts|tsx|jsx|json|md|txt|yaml|yml|csv|xlsx|docx|pptx)\b|"
    r"\b(?:api|trace|log|file|folder|directory|repo|git|pytest|curl|run|execute|test|debug|fix|install|pip|npm)\b|"
    r"(?:文件|目录|路径|日志|接口|运行|执行|测试|检查|读取|查看|修改|修复|代码|项目|程序|报错|错误|保存|写入|创建|安装)"
    r")"
)

_TOOL_ACTION_CLAIM_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:git|pytest|curl|pip|npm)\b|"
    r"(?:让我|我先|我来|现在我|先运行|运行一下|直接测试|测试API|执行命令|运行命令|命令执行|命令运行|"
    r"已成功|成功读取|已经读取|已读取|已查看|已检查|已修改|已修复|已写入|已保存|已创建|已执行|已运行|"
    r"已经查看|已经检查|已经修改|已经修复|已经写入|已经保存|已经创建|已经执行|已经运行|"
    r"检查完|读取完|查看完|修改完|修复完|写入完|保存完|创建完|测试通过|诊断日志|搞定|"
    r"ran|tested|read|checked|modified|wrote|saved|created|installed)"
    r")"
)

_TOOL_ACTION_CLAIM_MAX_CHARS = 180
_TOOL_ACTION_CLAIM_MAX_NONEMPTY_LINES = 3

_CHECKPOINT_SYSTEM_PREFIX = "【任务状态检查点】"
_QUESTION_TIMESTAMP_PREFIX = "提问时间："
_ANSWER_TIMESTAMP_PREFIX = "回答完成时间："
TIMESTAMP_SYSTEM_PREFIXES = (_QUESTION_TIMESTAMP_PREFIX, _ANSWER_TIMESTAMP_PREFIX)
_CONCEPTUAL_USER_RE = re.compile(r"(?i)(?:如何|怎么|怎样|为什么|解释|说明|\bhow\b|\bwhy\b|\bwhat\b|\bexplain\b)")


def _is_checkpoint_system_message(message: dict) -> bool:
    return (
        message.get("role") == "system"
        and str(message.get("content", "")).startswith(_CHECKPOINT_SYSTEM_PREFIX)
    )


def _is_timestamp_system_message(message: dict) -> bool:
    """识别随轮次沉淀的时间戳 system 消息（提问时间/回答完成时间）。

    这类消息需要像 user/assistant 一样随轮次保留在历史中，
    让模型能感知整段时间的流动，而不是只看到当轮时间。
    """
    return (
        message.get("role") == "system"
        and str(message.get("content", "")).startswith(TIMESTAMP_SYSTEM_PREFIXES)
    )


def _find_turn_timestamps(
    messages: list[dict], last_user_index: int, last_assistant_index: int
) -> Tuple[str, str]:
    """提取当轮的提问时间（紧贴最后一条 user 之前）与回答完成时间（最后一条 assistant 之后）。"""
    question_timestamp = ""
    if last_user_index > 0:
        before = messages[last_user_index - 1]
        if before.get("role") == "system" and str(before.get("content", "")).startswith(
            _QUESTION_TIMESTAMP_PREFIX
        ):
            question_timestamp = str(before.get("content", ""))

    answer_timestamp = ""
    if last_assistant_index >= 0:
        for message in messages[last_assistant_index + 1 :]:
            if message.get("role") == "system" and str(message.get("content", "")).startswith(
                _ANSWER_TIMESTAMP_PREFIX
            ):
                answer_timestamp = str(message.get("content", ""))
                break

    return question_timestamp, answer_timestamp


def compact_turn_for_next_context(turn_messages: list[dict]) -> list[dict]:
    """
    输入：当前轮完整 live_messages
    输出：下一轮允许携带的极简消息，只保留用户提问和最终成果回复；
    若当轮带有提问时间/回答完成时间 system 消息，一并保留，
    让时间戳随轮次在历史中沉淀（模型由此感知时间流动）。
    """
    user_content, last_user_index = _find_last_message_content(turn_messages, "user")
    assistant_content, last_assistant_index = _find_last_assistant_content(turn_messages)
    has_tool_evidence = _has_tool_evidence(turn_messages, start_index=last_user_index)

    if _assistant_content_too_short(assistant_content):
        assistant_content = _build_outcome_summary(turn_messages, start_index=last_user_index) or assistant_content
    elif (
        assistant_content
        and not has_tool_evidence
        and _turn_needs_tool_evidence(user_content)
        and _looks_like_tool_action_claim(assistant_content)
    ):
        assistant_content = ""

    question_timestamp, answer_timestamp = _find_turn_timestamps(
        turn_messages, last_user_index, last_assistant_index
    )

    compact_messages: List[Dict[str, str]] = []
    if question_timestamp and (user_content or assistant_content):
        compact_messages.append({"role": "system", "content": question_timestamp})
    if user_content:
        compact_messages.append({"role": "user", "content": user_content})
    if assistant_content:
        compact_messages.append({"role": "assistant", "content": assistant_content})
    if answer_timestamp and (user_content or assistant_content):
        compact_messages.append({"role": "system", "content": answer_timestamp})
    return compact_messages


def compact_history_for_next_context(messages: list[dict]) -> list[dict]:
    """Compact a restored display transcript into API-safe turn summaries.

    提问时间/回答完成时间 system 消息随各自轮次保留：
    - 提问时间属于"下一轮"，暂存后挂到下一条 user 所在轮的轮首；
    - 回答完成时间属于"当前轮"，并入当前轮消息序列，flush 时归位到轮尾。
    """
    compact_messages: List[Dict[str, str]] = []
    current_turn: List[dict] = []
    pending_question_timestamp = ""

    for message in messages or []:
        if not isinstance(message, dict):
            continue

        role = message.get("role")
        if role == "system":
            content = _content_to_text(message.get("content", "")).strip()
            if content.startswith(_CHECKPOINT_SYSTEM_PREFIX):
                compact_messages.append({"role": "system", "content": content})
                continue
            if content.startswith(_QUESTION_TIMESTAMP_PREFIX):
                pending_question_timestamp = content
                continue
            if content.startswith(_ANSWER_TIMESTAMP_PREFIX):
                if current_turn:
                    current_turn.append({"role": "system", "content": content})
                else:
                    compact_messages.append({"role": "system", "content": content})
            continue

        if role == "user":
            if current_turn:
                compact_messages.extend(compact_turn_for_next_context(current_turn))
            current_turn = [message]
            if pending_question_timestamp:
                current_turn.insert(0, {"role": "system", "content": pending_question_timestamp})
                pending_question_timestamp = ""
            continue

        if role in {"assistant", "tool"} and current_turn:
            current_turn.append(message)

    if current_turn:
        compact_messages.extend(compact_turn_for_next_context(current_turn))

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
    """裁剪紧凑历史：检查点 system 前置保留，正文按 user 轮次保留最近 max_turns 轮。

    轮内的提问时间/回答完成时间 system 消息随轮次一并保留（保持时间线交错结构），
    不再把所有非检查点 system 消息一刀切掉。
    """
    if max_turns <= 0:
        return []

    state_messages = [
        copy.deepcopy(message) for message in messages if _is_checkpoint_system_message(message)
    ]
    body_messages = [
        copy.deepcopy(message) for message in messages if not _is_checkpoint_system_message(message)
    ]

    user_seen = 0
    cut_index = 0
    for index in range(len(body_messages) - 1, -1, -1):
        if body_messages[index].get("role") == "user":
            user_seen += 1
            if user_seen == max_turns:
                cut_index = index
                break

    # 紧贴这轮 user 之前的提问时间 system 消息一并纳入保留范围
    # （只认提问时间前缀，避免把上一轮的回答完成时间也带进来）
    while cut_index > 0 and body_messages[cut_index - 1].get("role") == "system" and str(
        body_messages[cut_index - 1].get("content", "")
    ).startswith(_QUESTION_TIMESTAMP_PREFIX):
        cut_index -= 1

    return state_messages + body_messages[cut_index:]


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


def _has_tool_evidence(messages: list[dict], *, start_index: int) -> bool:
    scoped_messages = messages[start_index + 1 :] if start_index >= 0 else messages
    for message in scoped_messages:
        if message.get("role") == "tool":
            return True
        if message.get("role") == "assistant" and message.get("tool_calls"):
            return True
    return False


def _turn_needs_tool_evidence(user_content: str) -> bool:
    text = user_content or ""
    if not _TOOL_EVIDENCE_USER_RE.search(text):
        return False
    if _CONCEPTUAL_USER_RE.search(text) and not _PATH_RE.search(text):
        return False
    return True


def _looks_like_tool_action_claim(assistant_content: str) -> bool:
    text = _content_to_text(assistant_content).strip()
    if not text:
        return False
    if len(text) > _TOOL_ACTION_CLAIM_MAX_CHARS:
        return False

    nonempty_lines = [line for line in text.splitlines() if line.strip()]
    if len(nonempty_lines) > _TOOL_ACTION_CLAIM_MAX_NONEMPTY_LINES:
        return False

    return bool(_TOOL_ACTION_CLAIM_RE.search(text))


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
