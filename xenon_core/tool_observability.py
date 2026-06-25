from __future__ import annotations

import copy
import json
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from xenon_core.tool_payload_runtime import redact_base64_payloads


def safe_stringify_result(value: Any, max_chars: int = 1500) -> str:
    value = redact_base64_payloads(value)
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except Exception:
            text = str(value)

    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...<truncated>"


def build_tool_call_snapshot(
    *,
    tool_name: str,
    arguments: Dict[str, Any],
    success: bool,
    result: Any,
    phase: str,
    summarize_payload_fn: Callable[[str], str],
    error: str = "",
    recovery_summary: str = "",
) -> Dict[str, Any]:
    serialized_result = safe_stringify_result(result, max_chars=1200)
    summary = summarize_payload_fn(serialized_result)
    if not summary:
        summary = safe_stringify_result(serialized_result, max_chars=280)

    snapshot: Dict[str, Any] = {
        "tool_name": tool_name,
        "arguments": copy.deepcopy(arguments),
        "result": serialized_result,
        "result_summary": summary,
        "success": success,
        "timestamp": datetime.now().isoformat(),
        "phase": phase,
    }
    if error:
        snapshot["error"] = safe_stringify_result(error, max_chars=400)
    if recovery_summary:
        snapshot["recovery_summary"] = safe_stringify_result(recovery_summary, max_chars=400)
    return snapshot


def append_recent_tool_result(
    recent_results: List[Dict[str, Any]],
    snapshot: Dict[str, Any],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    if not snapshot:
        return copy.deepcopy(recent_results)

    updated_results = copy.deepcopy(recent_results)
    updated_results.append(copy.deepcopy(snapshot))
    if len(updated_results) > limit:
        updated_results = updated_results[-limit:]
    return updated_results


def get_recent_tool_results(
    recent_results: List[Dict[str, Any]],
    *,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    return copy.deepcopy(recent_results[-limit:])


def decay_recent_tool_results(
    recent_results: List[Dict[str, Any]],
    *,
    previous_user_input: str,
    user_input: str,
    similarity_fn: Callable[[str, str], float],
    same_topic_keep: int,
    topic_shift_keep: int,
    limit: int,
) -> List[Dict[str, Any]]:
    if not recent_results:
        return []

    keep_count = same_topic_keep
    if previous_user_input:
        similarity = similarity_fn(previous_user_input, user_input)
        if similarity < 0.35:
            keep_count = topic_shift_keep
    keep_count = max(1, min(keep_count, limit))
    return copy.deepcopy(recent_results[-keep_count:])


def serialize_alerts(alerts: Optional[List[Any]]) -> List[Dict[str, Any]]:
    serialized: List[Dict[str, Any]] = []
    for alert in alerts or []:
        if hasattr(alert, "alert_type"):
            serialized.append(
                {
                    "alert_type": getattr(alert, "alert_type", ""),
                    "severity": getattr(alert, "severity", ""),
                    "message": getattr(alert, "message", ""),
                    "suggestion": getattr(alert, "suggestion", ""),
                }
            )
        elif isinstance(alert, dict):
            serialized.append(copy.deepcopy(alert))
    return serialized


def collect_recent_failures(
    recent_results: List[Dict[str, Any]],
    *,
    blockage_reason: Optional[str] = None,
    limit: int = 3,
) -> List[str]:
    failures: List[str] = []

    if blockage_reason:
        failures.append(str(blockage_reason))

    for snapshot in reversed(recent_results):
        if snapshot.get("success") is False:
            failure_text = (
                snapshot.get("error")
                or snapshot.get("recovery_summary")
                or snapshot.get("result_summary")
                or snapshot.get("result")
                or ""
            )
            if failure_text:
                failures.append(str(failure_text))
        if len(failures) >= limit * 2:
            break

    deduped: List[str] = []
    for failure in failures:
        compact = str(failure).strip()
        if compact and compact not in deduped:
            deduped.append(compact)
        if len(deduped) >= limit:
            break
    return deduped
