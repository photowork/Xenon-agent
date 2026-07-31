from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


class ExecutionJournal:
    def __init__(self, journal_path: str = "Tasks/execution_journal.jsonl", max_entries: int = 20):
        self.journal_path = Path(journal_path)
        self.max_entries = max_entries
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)

    def log_planning(self, decision: Dict[str, Any], task_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "entry_type": "planning",
            "decision": decision,
            "task_state": task_state or {},
        }
        self._append(entry)
        return entry

    def log_tool_execution(
        self,
        goal: str,
        phase: str,
        tool_name: str,
        arguments: Dict[str, Any],
        result: Any,
        success: bool,
        recovery: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "entry_type": "tool_execution",
            "goal": goal,
            "phase": phase,
            "tool_name": tool_name,
            "arguments": self._truncate(arguments),
            "result": self._truncate(result),
            "success": success,
            "recovery": recovery or {},
        }
        self._append(entry)
        return entry

    def log_autonomous_tick(
        self,
        *,
        goal: str,
        phase: str,
        status: str,
        goal_source: str,
        next_actions: Optional[List[Any]] = None,
        stop_reason: Optional[str] = None,
        prompt_signature: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        replan_summary: Optional[Any] = None,
        memory_summary: Optional[str] = None,
    ) -> Dict[str, Any]:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "entry_type": "autonomous_tick",
            "goal": goal,
            "phase": phase,
            "status": status,
            "goal_source": goal_source,
            "next_actions": list(next_actions or []),
            "stop_reason": stop_reason,
            "prompt_signature": prompt_signature,
            "result": self._truncate(result or {}),
            "replan_summary": self._truncate(replan_summary or ""),
            "memory_summary": self._truncate(memory_summary or ""),
        }
        self._append(entry)
        return entry

    def log_multi_agent_event(
        self,
        *,
        event_type: str,
        run_id: str,
        parent_task_id: Optional[str],
        status: str,
        subtask_count: int,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "entry_type": "multi_agent",
            "event_type": event_type,
            "run_id": run_id,
            "parent_task_id": parent_task_id,
            "status": status,
            "subtask_count": subtask_count,
            "payload": self._truncate(payload or {}),
        }
        self._append(entry)
        return entry

    def get_recent_entries(self, limit: int = 10) -> List[Dict[str, Any]]:
        if not self.journal_path.exists():
            return []

        entries: List[Dict[str, Any]] = []
        with open(self.journal_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        return entries[-limit:]

    def summarize_recent(self, limit: int = 8) -> str:
        recent = self.get_recent_entries(limit=limit)
        if not recent:
            return ""

        lines = []
        for entry in recent:
            if entry.get("entry_type") == "planning":
                decision = entry.get("decision", {})
                lines.append(
                    f"[plan] phase={decision.get('phase')} mode={decision.get('mode')} "
                    f"tool={decision.get('selected_tool') or 'n/a'}"
                )
            elif entry.get("entry_type") == "tool_execution":
                lines.append(
                    f"[tool] {entry.get('tool_name')} success={entry.get('success')} "
                    f"phase={entry.get('phase')}"
                )
            elif entry.get("entry_type") == "autonomous_tick":
                lines.append(
                    f"[auto] status={entry.get('status')} phase={entry.get('phase')} "
                    f"source={entry.get('goal_source')} stop={entry.get('stop_reason') or '-'}"
                )
            elif entry.get("entry_type") == "multi_agent":
                lines.append(
                    f"[multi-agent] event={entry.get('event_type')} status={entry.get('status')} "
                    f"subtasks={entry.get('subtask_count')}"
                )
        return "\n".join(lines)

    def _append(self, entry: Dict[str, Any]) -> None:
        """追加条目并自动裁剪到 max_entries 条，防止文件无限增长。"""
        # 读取现有条目
        entries: List[Dict[str, Any]] = []
        if self.journal_path.exists():
            try:
                with open(self.journal_path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except (OSError, IOError):
                entries = []

        # 追加新条目
        entries.append(entry)

        # 只保留最后 max_entries 条
        entries = entries[-self.max_entries:]

        # 原子重写文件
        tmp_path = self.journal_path.with_suffix(".jsonl.tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            for e in entries:
                handle.write(json.dumps(e, ensure_ascii=False) + "\n")
        tmp_path.replace(self.journal_path)

    def _truncate(self, value: Any, max_chars: int = 1200) -> Any:
        if isinstance(value, (dict, list)):
            serialized = json.dumps(value, ensure_ascii=False)
            if len(serialized) <= max_chars:
                return value
            return serialized[:max_chars] + "...<truncated>"
        text = str(value)
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "...<truncated>"

    # Phase 4: 长任务恢复入口
    def save_recovery_point(
        self,
        task_id: str,
        phase: str,
        step_index: int,
        context_snapshot: Dict[str, Any],
        tool_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """保存恢复检查点，支持长任务中断后恢复。"""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "entry_type": "recovery_point",
            "task_id": task_id,
            "phase": phase,
            "step_index": step_index,
            "context_snapshot": context_snapshot,
            "tool_state": tool_state or {},
        }
        self._append(entry)
        return entry

    def get_latest_recovery_point(
        self, task_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """获取最近的恢复检查点，可按 task_id 过滤。"""
        entries = self.get_recent_entries(limit=self.max_entries)
        matching = [
            e for e in entries
            if e.get("entry_type") == "recovery_point"
        ]
        if task_id:
            matching = [e for e in matching if e.get("task_id") == task_id]
        return matching[-1] if matching else None

    def build_recovery_context(
        self, task_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """构建可用于恢复的执行上下文。
        返回最近恢复点的快照，包含 phase、step_index 和 tool_state，
        可供主循环重新接入执行。"""
        cp = self.get_latest_recovery_point(task_id)
        if not cp:
            return None
        return {
            "task_id": cp.get("task_id"),
            "phase": cp.get("phase"),
            "step_index": cp.get("step_index"),
            "context_snapshot": cp.get("context_snapshot", {}),
            "tool_state": cp.get("tool_state", {}),
            "recovered_at": cp.get("timestamp"),
        }
