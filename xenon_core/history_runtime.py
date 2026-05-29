from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def persist_full_history_snapshot(
    *,
    full_conversation_history: List[Dict[str, Any]],
    history_dir: Path,
    history_session_id: str,
    logger: Any,
    now: Optional[datetime] = None,
) -> Optional[Path]:
    try:
        active_history_dir = Path(history_dir)
        active_history_dir.mkdir(parents=True, exist_ok=True)
        current_time = now or datetime.now()
        payload = {
            "session_id": history_session_id,
            "updated_at": current_time.isoformat(),
            "message_count": len(full_conversation_history),
            "messages": full_conversation_history,
        }
        latest_path = active_history_dir / f"{history_session_id}_full_history.json"
        latest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return latest_path
    except Exception as error:
        logger.error("Failed to persist full conversation history: %s", error)
        return None





def save_api_request(
    *,
    enabled: bool,
    model: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    context_dir: Path,
    logger: Any,
    now: Optional[datetime] = None,
    max_files: int = 0,
    use_dated_subdir: bool = False,
) -> Optional[Path]:
    """Save API request to context_dir. If max_files > 0, keep only the N most recent."""
    if not enabled:
        return None

    try:
        current_time = now or datetime.now()
        active_context_dir = Path(context_dir)
        if use_dated_subdir:
            active_context_dir = active_context_dir / current_time.strftime("%Y-%m-%d")
        active_context_dir.mkdir(parents=True, exist_ok=True)
        timestamp = current_time.strftime("%Y%m%d_%H%M%S_%f")
        filepath = active_context_dir / f"api_request_{timestamp}.json"
        payload = {
            "timestamp": current_time.isoformat(),
            "model": model,
            "messages": messages,
            "tools": tools or [],
        }
        filepath.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("API请求已保存到: %s", filepath)

        # ── 保留最近 N 个文件 ────────────────────────────
        if max_files > 0:
            _trim_directory(active_context_dir, "api_request_*.json", max_files, logger)

        return filepath
    except Exception as error:
        logger.error("保存API请求失败: %s", error)
        return None


def save_turn_debug_trace(
    *,
    enabled: bool,
    turn_messages: List[Dict[str, Any]],
    trace_dir: Path,
    logger: Any,
    now: Optional[datetime] = None,
    metadata: Optional[Dict[str, Any]] = None,
    max_files: int = 0,
) -> Optional[Path]:
    """Persist a raw live turn trace for debugging without adding it to history."""
    if not enabled:
        return None

    try:
        current_time = now or datetime.now()
        active_trace_dir = Path(trace_dir) / current_time.strftime("%Y-%m-%d")
        active_trace_dir.mkdir(parents=True, exist_ok=True)
        timestamp = current_time.strftime("%Y%m%d_%H%M%S_%f")
        filepath = active_trace_dir / f"turn_trace_{timestamp}.json"
        payload = {
            "timestamp": current_time.isoformat(),
            "message_count": len(turn_messages),
            "messages": turn_messages,
            "metadata": metadata or {},
        }
        filepath.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Turn debug trace saved to: %s", filepath)

        if max_files > 0:
            _trim_directory(active_trace_dir, "turn_trace_*.json", max_files, logger)

        return filepath
    except Exception as error:
        logger.error("Failed to save turn debug trace: %s", error)
        return None


def _trim_directory(dir_path: Path, pattern: str, keep: int, logger: Any) -> None:
    """Keep only the most recent `keep` files matching pattern, delete the rest."""
    try:
        files = sorted(dir_path.glob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files[keep:]:
            f.unlink(missing_ok=True)
            logger.debug("已清理旧文件: %s", f.name)
    except Exception as e:
        logger.warning("清理旧API请求文件失败: %s", e)


def save_memory_log(
    *,
    enabled: bool,
    role: str,
    content: str = "",
    reasoning_content: str = "",
    memory_dir: Optional[Path] = None,
    logger: Any,
    now: Optional[datetime] = None,
) -> Optional[Path]:
    if not enabled:
        return None

    try:
        active_memory_dir = Path(memory_dir) if memory_dir is not None else Path("Memory/memory_log")
        active_memory_dir.mkdir(parents=True, exist_ok=True)
        current_time = now or datetime.now()
        timestamp = current_time.strftime("%Y%m%d_%H%M%S_%f")
        filepath = active_memory_dir / f"memory_{timestamp}.json"

        log_data: Dict[str, Any] = {
            "timestamp": current_time.isoformat(),
            "role": role,
        }
        if content:
            log_data["content"] = content
        if reasoning_content:
            log_data["reasoning_content"] = reasoning_content

        filepath.write_text(
            json.dumps(log_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return filepath
    except Exception as error:
        logger.error("保存memory_log失败: %s", error)
        return None
