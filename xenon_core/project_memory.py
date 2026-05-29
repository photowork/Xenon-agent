from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from xenon_core.model_request import build_chat_completion_kwargs

LEGACY_SUMMARY_PREFIX = "auto_summary_"
CHECKPOINT_SYSTEM_PREFIX = "【任务状态检查点】"
CHECKPOINT_SOURCE_MARKER = "来源: current-context-checkpoint-v2"
SUMMARY_SYSTEM_PREFIXES = (
    "【上下文压缩",
    CHECKPOINT_SYSTEM_PREFIX,
    "[PROJECT_MEMORY]",
    "銆愪笂涓嬫枃鍘嬬缉",
)


def get_project_memory_dir(base_dir: str = "Memory/auto_summary") -> Path:
    summary_dir = Path(base_dir)
    summary_dir.mkdir(parents=True, exist_ok=True)
    return summary_dir


def get_project_memory_path(
    *,
    project_memory_filename: str,
    summary_dir: Optional[Path] = None,
) -> Path:
    active_summary_dir = summary_dir or get_project_memory_dir()
    return active_summary_dir / project_memory_filename


def collect_summary_source_messages(
    source_messages: List[Dict[str, Any]],
    *,
    recent_limit: int = 20,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    recent = list(source_messages[-recent_limit:])

    last_user_message = None
    for message in reversed(source_messages):
        if message.get("role") == "user":
            last_user_message = message
            break

    if last_user_message and last_user_message not in recent:
        recent = [last_user_message] + recent

    dialog_messages = [message for message in recent if message.get("role") != "system"]
    return dialog_messages, last_user_message


def build_project_memory_text(
    *,
    smart_summary: str,
    last_user_message: Optional[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> str:
    current_time = now or datetime.now()
    summary_lines = [
        f"时间: {current_time.strftime('%Y-%m-%d %H:%M:%S')}",
        "类型: 任务状态检查点",
        CHECKPOINT_SOURCE_MARKER,
        "=" * 50,
        "",
        smart_summary,
    ]

    if last_user_message:
        summary_lines.extend(
            [
                "",
                "=" * 50,
                f"【用户当前问题】: {last_user_message.get('content', '')}",
            ]
        )

    return "\n".join(summary_lines)


def write_project_memory_files(
    *,
    summary_text: str,
    project_memory_filename: str,
    summary_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Tuple[Path, Path]:
    current_time = now or datetime.now()
    active_summary_dir = summary_dir or get_project_memory_dir()
    timestamp = current_time.strftime("%Y%m%d_%H%M%S")
    latest_path = active_summary_dir / project_memory_filename
    snapshot_path = active_summary_dir / f"{LEGACY_SUMMARY_PREFIX}{timestamp}.txt"

    for path in (latest_path, snapshot_path):
        path.write_text(summary_text, encoding="utf-8")

    return latest_path, snapshot_path


def load_previous_summary(
    *,
    project_memory_filename: str,
    summary_dir: Optional[Path] = None,
    logger: Any,
) -> str:
    try:
        active_summary_dir = summary_dir or get_project_memory_dir()
        memory_path = active_summary_dir / project_memory_filename
        if memory_path.exists():
            content = memory_path.read_text(encoding="utf-8").strip()
            if content:
                return content

        legacy_files = sorted(
            active_summary_dir.glob(f"{LEGACY_SUMMARY_PREFIX}*.txt"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for file_path in legacy_files[:1]:
            try:
                content = file_path.read_text(encoding="utf-8").strip()
                if content:
                    return content
            except Exception:
                continue

    except Exception as error:
        logger.error("Failed to load previous summary: %s", error)

    return ""


def extract_current_checkpoint(messages: List[Dict[str, Any]]) -> str:
    """Return the latest v2 checkpoint already present in the active context."""
    for message in reversed(messages):
        content = str(message.get("content", "") or "")
        if (
            message.get("role") == "system"
            and content.startswith(CHECKPOINT_SYSTEM_PREFIX)
            and CHECKPOINT_SOURCE_MARKER in content
        ):
            return content
    return ""


def inject_recent_memory_summary(
    *,
    messages: List[Dict[str, Any]],
    project_memory_filename: str,
    summary_injection_max_chars: int,
    include_user_question: bool,
    summary_dir: Optional[Path],
    logger: Any,
) -> None:
    """Inject the latest task checkpoint after leading system messages."""
    try:
        active_summary_dir = summary_dir or get_project_memory_dir()
        memory_path = active_summary_dir / project_memory_filename
        summary_content = ""

        if memory_path.exists():
            summary_content = memory_path.read_text(encoding="utf-8").strip()
        else:
            legacy_files = sorted(
                active_summary_dir.glob(f"{LEGACY_SUMMARY_PREFIX}*.txt"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:1]
            for file_path in legacy_files:
                try:
                    summary_content = file_path.read_text(encoding="utf-8").strip()
                except Exception:
                    continue

        messages[:] = [
            message
            for message in messages
            if not (
                message.get("role") == "system"
                and any(
                    str(message.get("content", "")).startswith(prefix)
                    for prefix in SUMMARY_SYSTEM_PREFIXES
                )
            )
        ]

        if not summary_content:
            return

        if len(summary_content) > summary_injection_max_chars:
            summary_content = summary_content[:summary_injection_max_chars].rstrip() + "..."

        checkpoint_lines = [
            CHECKPOINT_SYSTEM_PREFIX,
            "这是自动上下文压缩保存的当前任务状态。请基于它继续执行，不要要求用户重复上下文。",
            "",
            summary_content,
            "",
            "继续执行规则：",
            "- 继续推进 `# Remaining` / `# Current Focus` / `# Latest User Request` 中尚未完成的事项。",
            "- 不要重复 `# Completed` 中已经完成的工作。",
            "- 如需使用工具，基于检查点重新规划，不依赖已被清空的旧 tool_call 消息。",
            "- 如果 `# Completed` 已记录上下文清理/压缩已完成，不要因为 `# Latest User Request` 提到清理就再次调用清理工具；直接推进清理后的实质任务。",
        ]
        if include_user_question:
            checkpoint_lines.append("- 当前用户问题已包含在检查点中，按该目标继续。")

        checkpoint_message = {"role": "system", "content": "\n".join(checkpoint_lines)}

        insert_pos = 0
        while insert_pos < len(messages) and messages[insert_pos].get("role") == "system":
            insert_pos += 1
        messages.insert(insert_pos, checkpoint_message)

    except Exception as error:
        logger.error("注入任务状态检查点失败: %s", error)


def cleanup_old_summaries_if_healthy(
    *,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    context_manager: Any,
    project_memory_snapshot_prefix: str,
    summary_dir: Optional[Path],
    logger: Any,
) -> None:
    try:
        if not context_manager or not context_manager.token_counter:
            return

        current_tokens = context_manager.token_counter.estimate_total_tokens(messages, tools)
        healthy_threshold = context_manager.get_effective_max_tokens() * 0.5
        if current_tokens >= healthy_threshold:
            return

        active_summary_dir = summary_dir or get_project_memory_dir()

        snapshot_files = sorted(
            active_summary_dir.glob(f"{project_memory_snapshot_prefix}*.txt"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for snapshot in snapshot_files[3:]:
            try:
                snapshot.unlink()
                logger.info("Removed old project memory snapshot: %s", snapshot.name)
            except Exception as error:
                logger.error("Failed to remove snapshot %s: %s", snapshot.name, error)

        legacy_files = sorted(
            active_summary_dir.glob(f"{LEGACY_SUMMARY_PREFIX}*.txt"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for legacy in legacy_files[2:]:
            try:
                legacy.unlink()
                logger.info("Removed legacy summary file: %s", legacy.name)
            except Exception as error:
                logger.error("Failed to remove legacy summary %s: %s", legacy.name, error)

    except Exception as error:
        logger.error("Failed to clean old summaries: %s", error)


def build_summary_conversation(
    *,
    messages: List[Dict[str, Any]],
    recent_message_limit: int,
    max_message_snippet: int,
    summarize_tool_payload_fn: Callable[[str], str],
) -> str:
    parts = []

    for message in messages[-recent_message_limit:]:
        role = message.get("role", "")
        content = (message.get("content", "") or "").strip()

        if role == "user" and content:
            snippet = content[:max_message_snippet]
            if len(content) > max_message_snippet:
                snippet += "..."
            parts.append(f"[USER]\n{snippet}")
            continue

        if role == "assistant":
            reasoning = (message.get("reasoning_content", "") or "").strip()
            if reasoning:
                reasoning = reasoning[:200] + ("..." if len(reasoning) > 200 else "")
                parts.append(f"[ASSISTANT_REASONING]\n{reasoning}")
            if content:
                snippet = content[:max_message_snippet]
                if len(content) > max_message_snippet:
                    snippet += "..."
                parts.append(f"[ASSISTANT]\n{snippet}")
            continue

        if role == "tool" and content:
            snippet = summarize_tool_payload_fn(content)
            if not snippet:
                continue
            parts.append(f"[TOOL]\n{snippet}")

    return "\n\n".join(parts)


def _build_cognitive_state_injection(summary_dir: Optional[Path] = None) -> str:
    """Attempt to load cognitive network state from the network file near summary_dir."""
    try:
        base = Path(summary_dir or get_project_memory_dir()).parent
        network_path = base / "memory_network.json"
        if not network_path.exists():
            return ""
        from xenon_core.cognitive_network import CognitiveNetworkState
        builder = CognitiveNetworkState(str(network_path))
        cognitive_state = builder.build_summary(max_nodes=6, max_chars=1200)
        if cognitive_state:
            return "\n\nLong-term memory (cognitive network) — use these patterns to inform the checkpoint:\n" + cognitive_state
    except Exception:
        pass
    return ""


def build_project_memory_prompt(
    *,
    previous_summary: str,
    conversation_text: str,
    cognitive_state_text: str = "",
) -> str:
    cognitive_block = f"\n\n{cognitive_state_text}" if cognitive_state_text else ""

    if previous_summary:
        return f"""You maintain a task checkpoint for a coding agent after automatic context compression.

Update the checkpoint using the previous checkpoint plus the current conversation.
This checkpoint is the only state that will survive compression, so preserve the execution state precisely.

Priority order:
1. The user's original goal and acceptance criteria
2. What has been done in this task
3. What is confirmed completed and should not be repeated
4. What is still unfinished, blocked, or risky
5. Key files, commands, test results, errors, and concrete tool findings
6. Decisions, constraints, and user preferences that affect the next step
7. The exact next actions the agent should take after compression
8. Treat tool results as temporary working memory: keep only conclusions, paths, errors, and facts
9. If context cleanup/compaction has just been completed, record it as completed and move the next action to the substantive post-cleanup task; do not schedule another cleanup just because the latest request mentioned cleanup.

If the new conversation conflicts with old memory, prefer the new conversation.
Remove chatter and repeated wording. Keep the summary compact but actionable.

Output exactly these Markdown headings, in Chinese content where appropriate:
# Goals
# Constraints
# Decisions
# Completed
# Remaining
# Current Focus
# Key Files
# Latest User Request{cognitive_block}

Previous checkpoint:
{previous_summary}

Current conversation before compression:
{conversation_text}
"""

    return f"""You are creating a task checkpoint for a coding agent after automatic context compression.

This checkpoint is the only state that will survive compression. Extract the current task state, not a chat transcript.

Keep:
1. The user's original goal and acceptance criteria
2. What has been done in this task
3. What is confirmed completed and should not be repeated
4. What is still unfinished, blocked, or risky
5. Key files, commands, test results, errors, and concrete tool findings
6. Decisions, constraints, and user preferences that affect the next step
7. The exact next actions the agent should take after compression
8. Treat tool results as temporary working memory: keep only conclusions, paths, errors, and facts
9. If context cleanup/compaction has just been completed, record it as completed and move the next action to the substantive post-cleanup task; do not schedule another cleanup just because the latest request mentioned cleanup.

Output exactly these Markdown headings, in Chinese content where appropriate:
# Goals
# Constraints
# Decisions
# Completed
# Remaining
# Current Focus
# Key Files
# Latest User Request{cognitive_block}

Current conversation before compression:
{conversation_text}
"""


def generate_rule_summary(
    *,
    messages: List[Dict[str, Any]],
    recent_message_limit: int,
    max_tool_snippet: int,
    summarize_tool_payload_fn: Callable[[str], str],
) -> str:
    user_requests = []
    assistant_updates = []
    tool_results = []

    for message in messages[-recent_message_limit:]:
        role = message.get("role", "")
        content = (message.get("content", "") or "").replace("\n", " ").strip()
        reasoning = (message.get("reasoning_content", "") or "").replace("\n", " ").strip()
        if not content and not reasoning:
            continue

        limit = max_tool_snippet if role == "tool" else 240
        preview = content[:limit]
        if len(content) > limit:
            preview += "..."

        if role == "user":
            user_requests.append(f"- {preview}")
        elif role == "assistant":
            if reasoning:
                reasoning_preview = reasoning[:240]
                if len(reasoning) > 240:
                    reasoning_preview += "..."
                assistant_updates.append(f"- [reasoning] {reasoning_preview}")
            if preview:
                assistant_updates.append(f"- {preview}")
        elif role == "tool":
            tool_preview = summarize_tool_payload_fn(content) or preview
            tool_results.append(f"- {tool_preview}")

    latest_request = user_requests[-1] if user_requests else "- no explicit request recorded"

    sections = [
        "# Goals",
        "Unknown",
        "",
        "# Constraints",
        "Unknown",
        "",
        "# Decisions",
        "Unknown",
        "",
        "# Completed",
        *(assistant_updates[-5:] or ["Unknown"]),
        "",
        "# Remaining",
        "Continue from the latest user request and recover missing long-term constraints manually if needed.",
        "",
        "# Current Focus",
        latest_request,
        "",
        "# Key Files",
        *(tool_results[-5:] or ["Unknown"]),
        "",
        "# Latest User Request",
        latest_request,
    ]
    return "\n".join(sections)


def generate_smart_summary(
    *,
    messages: List[Dict[str, Any]],
    project_memory_filename: str,
    summary_dir: Optional[Path],
    recent_message_limit: int,
    max_message_snippet: int,
    max_tool_snippet: int,
    summarize_tool_payload_fn: Callable[[str], str],
    openai_client_cls: Any,
    api_key: str,
    base_url: str,
    summary_timeout: Any,
    summary_model: str,
    summary_max_tokens: int,
    logger: Any,
    summary_thinking_enabled: Optional[bool] = False,
    summary_reasoning_effort: Optional[str] = None,
    include_previous_summary: bool = True,
    previous_summary_override: Optional[str] = None,
) -> str:
    if previous_summary_override is not None:
        previous_summary = previous_summary_override
    elif include_previous_summary:
        previous_summary = load_previous_summary(
            project_memory_filename=project_memory_filename,
            summary_dir=summary_dir,
            logger=logger,
        )
    else:
        previous_summary = ""
    conversation_text = build_summary_conversation(
        messages=messages,
        recent_message_limit=recent_message_limit,
        max_message_snippet=max_message_snippet,
        summarize_tool_payload_fn=summarize_tool_payload_fn,
    )

    if not conversation_text.strip():
        return previous_summary

    cognitive_state_text = _build_cognitive_state_injection(summary_dir)
    prompt = build_project_memory_prompt(
        previous_summary=previous_summary,
        conversation_text=conversation_text,
        cognitive_state_text=cognitive_state_text,
    )

    try:
        summary_client = openai_client_cls(
            api_key=api_key,
            base_url=base_url,
            timeout=summary_timeout,
        )
        try:
            request_kwargs = build_chat_completion_kwargs(
                model=summary_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Create a compact task checkpoint for automatic context compression. "
                            "Preserve the user's goal, completed work, concrete findings, blockers, and next actions."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=summary_max_tokens,
                temperature=0.2,
                thinking_enabled=summary_thinking_enabled,
                reasoning_effort=summary_reasoning_effort,
            )
            response = summary_client.chat.completions.create(**request_kwargs)
        finally:
            close_fn = getattr(summary_client, "close", None)
            if callable(close_fn):
                close_fn()

        summary = ((response.choices[0].message.content or "").strip() if response.choices else "")
        if summary:
            logger.info("Project memory generated successfully, length: %s", len(summary))
            return summary

        logger.error("Project memory generation returned empty content")

    except Exception as error:
        logger.error("Project memory generation failed: %s", error)

    return generate_rule_summary(
        messages=messages,
        recent_message_limit=recent_message_limit,
        max_tool_snippet=max_tool_snippet,
        summarize_tool_payload_fn=summarize_tool_payload_fn,
    )


def inject_cognitive_state(
    *,
    messages: List[Dict[str, Any]],
    current_query: str = "",
    current_phase: str = "",
    current_intent: str = "",
    max_nodes: int = 5,
    summary_dir: Optional[Path] = None,
    logger: Any = None,
) -> bool:
    """Inject the most relevant long-term memories as a system message.

    Loads the cognitive network state (activation set) from the memory graph
    and inserts it after the leading system messages so the agent is aware
    of relevant past patterns without manual retrieval.

    Call this before each LLM turn to keep the agent contextually grounded.
    """
    try:
        base = Path(summary_dir or get_project_memory_dir()).parent
        network_path = base / "memory_network.json"
        if not network_path.exists():
            return False

        from xenon_core.cognitive_network import CognitiveNetworkState

        builder = CognitiveNetworkState(str(network_path))
        activation_set = builder.get_activation_set(
            current_query=current_query,
            current_phase=current_phase,
            current_intent=current_intent,
            limit=max_nodes,
        )
        if not activation_set:
            return False

        lines = ["[COGNITIVE_STATE]", "Relevant long-term memories:"]
        for item in activation_set:
            summary = str(item.get("summary", "")).strip()
            ctype = str(item.get("cognitive_type", "")).strip()
            tags = item.get("tags", [])
            tag_str = ", ".join(tags[:3]) if tags else ""
            if summary:
                entry = f"- [{ctype}] {summary}"
                if tag_str:
                    entry += f" (tags: {tag_str})"
                lines.append(entry)

        cognitive_message = {"role": "system", "content": "\n".join(lines)}

        # Insert after existing system messages
        insert_pos = 0
        while insert_pos < len(messages) and messages[insert_pos].get("role") == "system":
            insert_pos += 1
        messages.insert(insert_pos, cognitive_message)

        return True
    except Exception as error:
        if logger:
            logger.error("Failed to inject cognitive state: %s", error)
        return False


def emergency_context_clear(
    *,
    messages: List[Dict[str, Any]],
    include_user_question: bool,
    save_context_summary_fn: Callable[[List[Dict[str, Any]]], None],
    inject_recent_memory_summary_fn: Callable[[List[Dict[str, Any]], bool], None],
    extract_tool_call_id_fn: Callable[[Any], Optional[str]],
    logger: Any,
) -> None:
    save_context_summary_fn(messages)

    system_messages = [message for message in messages if message.get("role") == "system"]

    messages.clear()
    messages.extend(system_messages)
    inject_recent_memory_summary_fn(messages, include_user_question)

    logger.info("已执行紧急清空，保留系统消息和任务状态检查点")
