from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

from xenon_core.turn_compactor import compact_history_for_next_context


DEFAULT_RECENT_TURNS_AFTER_TRIM = 3


def analyze_context_weight(
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]] | None = None,
    *,
    top_n: int = 8,
    token_counter: Any = None,
) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    total_message_chars = 0
    total_estimated_tokens = 0

    for index, message in enumerate(messages):
        serialized = json.dumps(message, ensure_ascii=False, default=str)
        chars = len(serialized)
        tokens = _estimate_tokens(serialized, token_counter)
        total_message_chars += chars
        total_estimated_tokens += tokens
        entries.append(
            {
                "index": index,
                "role": message.get("role"),
                "chars": chars,
                "tokens": tokens,
                "tool_call_id": message.get("tool_call_id"),
                "has_tool_calls": bool(message.get("tool_calls")),
                "preview": str(message.get("content", ""))[:160],
            }
        )

    tools = tools or []
    serialized_tools = json.dumps(tools, ensure_ascii=False, default=str)
    tool_schema_chars = len(serialized_tools)
    tool_schema_tokens = _estimate_tokens(serialized_tools, token_counter) if tools else 0

    return {
        "message_count": len(messages),
        "tool_schema_count": len(tools),
        "total_message_chars": total_message_chars,
        "tool_schema_chars": tool_schema_chars,
        "estimated_total_tokens": total_estimated_tokens + tool_schema_tokens,
        "top_messages": sorted(entries, key=lambda item: item["chars"], reverse=True)[:top_n],
    }


def _estimate_tokens(text: str, token_counter: Any = None) -> int:
    if token_counter and hasattr(token_counter, "count_tokens"):
        return int(token_counter.count_tokens(text))
    return max(1, len(text) // 4)


def ensure_context_size(
    *,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    context_manager: Any,
    auto_trim_context_fn: Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], None],
    logger: Any,
    print_fn: Callable[..., Any] = print,
) -> bool:
    if not context_manager or not context_manager.token_counter:
        return False

    total_tokens = context_manager.token_counter.estimate_total_tokens(messages, tools)
    max_tokens = context_manager.get_effective_max_tokens()

    if not context_manager.should_trigger_cleanup(total_tokens):
        return False

    ratio = total_tokens / max_tokens
    logger.info("Token usage %.1f%% exceeded trim threshold", ratio * 100)
    print_fn(f"\n\033[93m[自动裁剪] Token 使用率 {ratio:.1%}，正在删除旧对话轮次...\033[0m")

    auto_trim_context_fn(messages, tools)

    new_tokens = context_manager.token_counter.estimate_total_tokens(messages, tools)
    new_ratio = new_tokens / max_tokens
    logger.info("Simple context trim finished: token usage %.1f%%", new_ratio * 100)
    print_fn(f"\033[93m[裁剪完成] 当前使用率 {new_ratio:.1%}\033[0m")
    return True


def auto_trim_context(
    *,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    context_manager: Any,
    protected_conversation_rounds: int,
    extract_tool_call_id_fn: Callable[[Any], Any],
    save_context_summary_fn: Callable[[List[Dict[str, Any]]], None],
    compress_tool_messages_fn: Callable[..., int],
    inject_recent_memory_summary_fn: Callable[[List[Dict[str, Any]]], None],
    emergency_context_clear_fn: Callable[[List[Dict[str, Any]], bool], None],
    logger: Any,
) -> None:
    if not context_manager or not context_manager.token_counter:
        return

    max_tokens = context_manager.get_effective_max_tokens()
    before_tokens = context_manager.token_counter.estimate_total_tokens(messages, tools)
    logger.info(
        "Simple context trim: before_tokens=%s, max_tokens=%s, messages=%s",
        before_tokens,
        max_tokens,
        len(messages),
    )

    keep_turns = _normalize_keep_turns(protected_conversation_rounds)
    removed_count = trim_messages_to_recent_context(
        messages,
        keep_recent_turns=keep_turns,
    )

    after_tokens = context_manager.token_counter.estimate_total_tokens(messages, tools)
    while after_tokens >= max_tokens and keep_turns > 0:
        keep_turns -= 1
        removed_count += trim_messages_to_recent_context(
            messages,
            keep_recent_turns=keep_turns,
        )
        after_tokens = context_manager.token_counter.estimate_total_tokens(messages, tools)

    if after_tokens >= max_tokens:
        logger.warning(
            "Simple context trim still exceeds API limit (%s >= %s). Current live turn, system prompt, or tool schema may be too large.",
            after_tokens,
            max_tokens,
        )
    if removed_count > 0:
        logger.info("Simple context trim removed %s old history message(s)", removed_count)
    logger.info(
        "Simple context trim complete: after_tokens=%s, messages=%s, keep_recent_turns=%s",
        after_tokens,
        len(messages),
        keep_turns,
    )


def trim_messages_to_recent_context(
    messages: List[Dict[str, Any]],
    *,
    keep_recent_turns: int = DEFAULT_RECENT_TURNS_AFTER_TRIM,
) -> int:
    """Drop oldest compact history while preserving system state and the live turn."""
    original_count = len(messages)
    if not messages:
        return 0

    keep_recent_turns = _normalize_keep_turns(keep_recent_turns)
    current_turn_start = _find_latest_user_index(messages)

    if current_turn_start >= 0:
        history_source = messages[:current_turn_start]
        current_turn = messages[current_turn_start:]
    else:
        history_source = messages
        current_turn = []

    system_messages = _dedupe_messages(
        message
        for message in messages
        if message.get("role") == "system"
    )
    recent_history = _recent_compact_history(history_source, keep_recent_turns)
    live_turn = [
        message
        for message in current_turn
        if message.get("role") != "system"
    ]

    messages[:] = system_messages + recent_history + live_turn
    return max(0, original_count - len(messages))


def _recent_compact_history(
    history_source: List[Dict[str, Any]],
    keep_recent_turns: int,
) -> List[Dict[str, Any]]:
    if keep_recent_turns <= 0:
        return []

    sanitized = compact_history_for_next_context(history_source)
    compact = [
        message
        for message in sanitized
        if message.get("role") in {"user", "assistant"}
        and str(message.get("content", "")).strip()
    ]
    return compact[-keep_recent_turns * 2 :]


def _find_latest_user_index(messages: List[Dict[str, Any]]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    return -1


def _normalize_keep_turns(value: Any) -> int:
    try:
        return max(0, int(value))
    except Exception:
        return DEFAULT_RECENT_TURNS_AFTER_TRIM


def _dedupe_messages(messages: Any) -> List[Dict[str, Any]]:
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for message in messages:
        marker = (
            message.get("role"),
            str(message.get("content", "")),
        )
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(message)
    return deduped
