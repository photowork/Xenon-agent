from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional


class RecoveryManager:
    def build_recovery_plan(
        self,
        tool_name: str,
        error: Any,
        phase: Optional[str] = None,
    ) -> Dict[str, Any]:
        error_text = str(error).lower()
        blockage_reason = "tool_failed"
        next_actions = ["retry_same_tool", "search_more_context"]
        should_retry = True
        summary = f"{tool_name} failed during {phase or 'unknown'}."

        if any(token in error_text for token in ["timeout", "timed out", "network", "dns"]):
            blockage_reason = "tool_timeout"
            next_actions = ["retry_same_tool", "switch_to_terminal", "ask_user"]
            summary = f"{tool_name} timed out; retry or switch execution path."
        elif any(token in error_text for token in ["ssh", "remote", "connection", "unreachable"]):
            blockage_reason = "remote_unreachable"
            next_actions = ["retry_same_tool", "switch_to_local_analysis", "ask_user"]
            summary = f"{tool_name} hit a remote connectivity issue."
        elif any(token in error_text for token in ["json", "syntax", "parse", "argument"]):
            blockage_reason = "syntax_error"
            next_actions = ["fix_arguments", "retry_same_tool", "search_more_context"]
            should_retry = False
            summary = f"{tool_name} failed because its arguments or payload look invalid."
        elif any(token in error_text for token in ["token", "context", "too long", "maximum context"]):
            blockage_reason = "context_overflow"
            next_actions = ["trim_context", "search_more_context", "retry_same_tool"]
            should_retry = False
            summary = "Context pressure caused a failure; trim and resume."
        elif any(token in error_text for token in ["permission", "access", "denied"]):
            blockage_reason = "permission_denied"
            next_actions = ["switch_tool", "ask_user"]
            should_retry = False
            summary = f"{tool_name} failed because of a permission boundary."

        return {
            "timestamp": datetime.now().isoformat(),
            "tool_name": tool_name,
            "phase": phase,
            "blockage_reason": blockage_reason,
            "next_actions": next_actions,
            "should_retry": should_retry,
            "summary": summary,
            "error": str(error),
        }
