from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


COGNITIVE_NETWORK_PREFIX = "[COGNITIVE_NETWORK]"


def get_cognitive_network_summary(
    *,
    cognitive_network: Any,
    cached_summary: str,
    logger: Any,
    set_cached_summary_fn: Callable[[str], None],
    current_query: Optional[str] = None,
    current_phase: Optional[str] = None,
    current_intent: Optional[str] = None,
    recent_failures: Optional[List[str]] = None,
) -> str:
    """Build and cache a compact cognitive-state summary."""
    try:
        if current_phase or current_intent or recent_failures:
            summary = cognitive_network.build_phase_summary(
                current_query=current_query,
                current_phase=current_phase,
                current_intent=current_intent,
                recent_failures=recent_failures or [],
            )
        else:
            summary = cognitive_network.build_summary(current_query=current_query)
        if summary:
            set_cached_summary_fn(summary)
        return summary
    except Exception as error:
        logger.error("Failed to build cognitive network summary: %s", error)
        return cached_summary


def inject_cognitive_network_summary(
    *,
    messages: List[Dict[str, Any]],
    get_cognitive_network_summary_fn: Callable[..., str],
    current_query: Optional[str] = None,
    current_phase: Optional[str] = None,
    current_intent: Optional[str] = None,
    recent_failures: Optional[List[str]] = None,
) -> None:
    """Inject the cognitive network as a persistent system-level signal."""
    summary = get_cognitive_network_summary_fn(
        current_query=current_query,
        current_phase=current_phase,
        current_intent=current_intent,
        recent_failures=recent_failures,
    )
    if not summary:
        return

    summary_message = {
        "role": "system",
        "content": (
            f"{COGNITIVE_NETWORK_PREFIX}\n"
            "This is Xenon's persistent cognitive state distilled from the evolving memory network. "
            "Let it influence priorities, attention, recall, and planning. "
            "Prefer the newest user message if it directly conflicts with older network state.\n\n"
            f"{summary}"
        ),
    }

    messages[:] = [
        message
        for message in messages
        if not (
            message.get("role") == "system"
            and message.get("content", "").startswith(COGNITIVE_NETWORK_PREFIX)
        )
    ]

    # 插入到最后一条 user 消息之前（即对话历史之后、最新 user 之前）。
    # 这样前缀缓存可以覆盖 [静态提示词 + 完整对话历史] 这段不变的前缀，
    # COG 作为每轮变化的动态信号放在尾部，紧邻最新 user，既保持对当前输入的影响权重，又不破坏缓存命中。
    insert_pos = len(messages)
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].get("role") == "user":
            insert_pos = idx
            break
    messages.insert(insert_pos, summary_message)
