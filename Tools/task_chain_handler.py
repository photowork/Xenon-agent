import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from xenon_core.phase_policy import normalize_phase_state
from datetime import datetime
import uuid

_UNSET = object()


class TaskChainToolManager:
    def __init__(self):
        self.tasks_dir = Path("Tasks")
        self.active_dir = self.tasks_dir / "active"
        self.archived_dir = self.tasks_dir / "archived"
        self.current_task_file = self.active_dir / "current_task.json"

        self._ensure_directories()

    def _ensure_directories(self):
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.active_dir.mkdir(parents=True, exist_ok=True)
        self.archived_dir.mkdir(parents=True, exist_ok=True)

    def _generate_task_id(self) -> str:
        return str(uuid.uuid4())[:8]

    def _default_execution_state(self) -> Dict[str, Any]:
        return {
            "phase": "plan",
            "recovery_mode": "none",
            "blockage_reason": None,
            "next_actions": [],
            "mode": "single_step",
            "reasoning_summary": "",
            "last_tool": None,
            "last_tool_result": None,
            "last_replan": None,
            "last_reflection": None,
            "last_alerts": [],
            "updated_at": datetime.now().isoformat(),
        }

    def _default_autonomous_state(self) -> Dict[str, Any]:
        return {
            "status": "idle",
            "phase_stagnation_count": 0,
            "repeated_action_count": 0,
            "tool_failure_streak": 0,
            "last_phase": None,
            "last_next_actions": [],
            "last_goal_source": None,
            "last_prompt_signature": None,
            "last_tick_at": None,
            "last_result": None,
            "stop_reason": None,
            "updated_at": datetime.now().isoformat(),
        }

    def _normalize_next_actions(self, next_actions: Optional[List[str]]) -> List[str]:
        if not next_actions:
            return []
        return [str(action) for action in next_actions if str(action).strip()]

    def _ensure_task_shape(self, task: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(task)
        normalized.setdefault("metadata", {})
        normalized.setdefault("steps", [])
        execution_state = normalized.setdefault("execution_state", self._default_execution_state())
        for key, value in self._default_execution_state().items():
            execution_state.setdefault(key, value)
        autonomous_state = normalized.setdefault("autonomous_state", self._default_autonomous_state())
        for key, value in self._default_autonomous_state().items():
            autonomous_state.setdefault(key, value)

        for step in normalized["steps"]:
            step.setdefault("notes", "")
            step.setdefault("output", "")
            step.setdefault("started_at", None)
            step.setdefault("completed_at", None)
            step.setdefault("dependencies", [])
            step.setdefault("phase", "pending")
            step.setdefault("blockage_reason", None)
            step.setdefault("next_actions", [])

        return normalized

    def _save_task(self, task: Dict[str, Any]) -> bool:
        try:
            task = self._ensure_task_shape(task)
            with open(self.current_task_file, "w", encoding="utf-8") as handle:
                json.dump(task, handle, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"保存任务失败: {e}")
            return False

    def _load_task(self) -> Optional[Dict[str, Any]]:
        try:
            if not self.current_task_file.exists():
                return None

            with open(self.current_task_file, "r", encoding="utf-8") as handle:
                return self._ensure_task_shape(json.load(handle))
        except Exception as e:
            print(f"加载任务失败: {e}")
            return None

    def _archive_task(self, task: Dict[str, Any]) -> bool:
        try:
            task = self._ensure_task_shape(task)
            task_id = task.get("task_id", "unknown")
            archive_file = self.archived_dir / f"task_{task_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

            with open(archive_file, "w", encoding="utf-8") as handle:
                json.dump(task, handle, ensure_ascii=False, indent=2)

            if self.current_task_file.exists():
                self.current_task_file.unlink()

            return True
        except Exception as e:
            print(f"归档任务失败: {e}")
            return False

    def start_task(
        self,
        description: str,
        steps: List[str],
        objective: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        task_id = self._generate_task_id()

        task = {
            "task_id": task_id,
            "description": description,
            "objective": objective or description,
            "status": "in_progress",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "steps": [],
            "current_step": 0,
            "metadata": metadata or {},
            "execution_state": self._default_execution_state(),
            "autonomous_state": self._default_autonomous_state(),
        }

        for idx, step_desc in enumerate(steps, 1):
            task["steps"].append(
                {
                    "step_id": idx,
                    "description": step_desc,
                    "status": "pending",
                    "notes": "",
                    "output": "",
                    "started_at": None,
                    "completed_at": None,
                    "dependencies": [],
                    "phase": "pending",
                    "blockage_reason": None,
                    "next_actions": [],
                }
            )

        self._save_task(task)

        print(f"任务已创建: {task_id}")
        print(f"  描述: {description}")
        print(f"  步骤数: {len(steps)}")
        return task_id

    def ensure_task(
        self,
        description: str,
        objective: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        steps: Optional[List[str]] = None,
    ) -> str:
        current = self._load_task()
        if current and current.get("status") == "in_progress":
            return current["task_id"]

        default_steps = steps or [
            "Analyze the request",
            "Locate relevant files or tools",
            "Execute the required action",
            "Verify and summarize the result",
        ]
        return self.start_task(
            description=description,
            steps=default_steps,
            objective=objective,
            metadata=metadata,
        )

    def get_current_task(self) -> Optional[Dict[str, Any]]:
        task = self._load_task()
        if not task:
            return None

        return {
            "task": task,
            "task_id": task["task_id"],
            "description": task["description"],
            "objective": task["objective"],
            "status": task["status"],
            "current_step": task["current_step"],
            "total_steps": len(task["steps"]),
            "progress": f"{task['current_step']}/{len(task['steps'])}",
            "next_step": self._get_next_step(task),
            "completed_steps": [step for step in task["steps"] if step["status"] == "completed"],
            "execution_state": task.get("execution_state", self._default_execution_state()),
            "autonomous_state": task.get("autonomous_state", self._default_autonomous_state()),
        }

    def get_task_progress(self) -> Optional[Dict[str, Any]]:
        task = self._load_task()
        if not task:
            return None

        total_steps = len(task["steps"])
        completed_steps = sum(1 for step in task["steps"] if step["status"] == "completed")
        in_progress_steps = sum(1 for step in task["steps"] if step["status"] == "in_progress")
        pending_steps = sum(1 for step in task["steps"] if step["status"] == "pending")

        return {
            "task_id": task["task_id"],
            "description": task["description"],
            "total_steps": total_steps,
            "completed_steps": completed_steps,
            "in_progress_steps": in_progress_steps,
            "pending_steps": pending_steps,
            "progress_percentage": (completed_steps / total_steps * 100) if total_steps > 0 else 0,
            "current_step": task["current_step"],
            "execution_state": task.get("execution_state", self._default_execution_state()),
            "autonomous_state": task.get("autonomous_state", self._default_autonomous_state()),
        }

    def _get_next_step(self, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for step in task["steps"]:
            if step["status"] in ["pending", "in_progress"]:
                return step
        return None

    def get_next_step(self) -> Optional[Dict[str, Any]]:
        task = self._load_task()
        if not task:
            return None
        return self._get_next_step(task)

    def update_step(
        self,
        step_id: int,
        status: str,
        notes: Optional[str] = None,
        output: Optional[str] = None,
        phase: Optional[str] = None,
        blockage_reason: Optional[str] = None,
        next_actions: Optional[List[str]] = None,
    ) -> bool:
        task = self._load_task()
        if not task:
            print("没有活跃的任务")
            return False

        step_found = False
        for step in task["steps"]:
            if step["step_id"] == step_id:
                step["status"] = status
                if notes is not None:
                    step["notes"] = notes
                if output is not None:
                    step["output"] = output
                if phase is not None:
                    step["phase"] = phase
                if blockage_reason is not None:
                    step["blockage_reason"] = blockage_reason
                if next_actions is not None:
                    step["next_actions"] = self._normalize_next_actions(next_actions)

                if status == "in_progress" and not step["started_at"]:
                    step["started_at"] = datetime.now().isoformat()
                elif status == "completed":
                    step["completed_at"] = datetime.now().isoformat()

                step_found = True
                break

        if not step_found:
            print(f"未找到步骤 {step_id}")
            return False

        task["updated_at"] = datetime.now().isoformat()
        if status == "completed":
            task["current_step"] = step_id
            self._save_task(task)

            # 🔁 自动检测：所有步骤已完成 → 自动归档关闭任务
            all_done = all(s["status"] == "completed" for s in task["steps"])
            if all_done and task.get("status") != "completed":
                self.complete_task("所有步骤已完成，任务自动关闭。")
                print(f"  ✓ 所有步骤已完成，任务 {task['task_id']} 已自动关闭")
            else:
                print(f"步骤 {step_id} 已更新: {status}")
            return True

        elif status == "in_progress" and task["current_step"] < step_id:
            task["current_step"] = step_id - 1

        self._save_task(task)
        print(f"步骤 {step_id} 已更新: {status}")
        return True

    def mark_step_completed(self, step_id: int, notes: Optional[str] = None, output: Optional[str] = None) -> bool:
        return self.update_step(step_id, "completed", notes, output)

    def mark_step_failed(self, step_id: int, error: str) -> bool:
        return self.update_step(step_id, "failed", notes=f"错误: {error}")

    def mark_step_in_progress(self, step_id: int) -> bool:
        return self.update_step(step_id, "in_progress")

    def get_step_history(self) -> Optional[List[Dict[str, Any]]]:
        task = self._load_task()
        if not task:
            return None
        return task["steps"]

    def sync_execution_state(
        self,
        phase: Any = _UNSET,
        recovery_mode: Any = _UNSET,
        blockage_reason: Any = _UNSET,
        next_actions: Any = _UNSET,
        mode: Any = _UNSET,
        reasoning_summary: Any = _UNSET,
        last_tool: Any = _UNSET,
        last_tool_result: Any = _UNSET,
        last_replan: Any = _UNSET,
        last_reflection: Any = _UNSET,
        last_alerts: Any = _UNSET,
    ) -> bool:
        task = self._load_task()
        if not task:
            return False

        execution_state = task.setdefault("execution_state", self._default_execution_state())
        if phase is not _UNSET or recovery_mode is not _UNSET:
            phase_value = execution_state.get("phase", "plan") if phase is _UNSET else phase
            recovery_value = execution_state.get("recovery_mode", "none") if recovery_mode is _UNSET else recovery_mode
            normalized = normalize_phase_state(phase_value, recovery_value)
            execution_state["phase"] = normalized["phase"]
            execution_state["recovery_mode"] = normalized["recovery_mode"]
        if blockage_reason is not _UNSET:
            execution_state["blockage_reason"] = blockage_reason
        if next_actions is not _UNSET:
            execution_state["next_actions"] = self._normalize_next_actions(next_actions)
        if mode is not _UNSET:
            execution_state["mode"] = mode
        if reasoning_summary is not _UNSET:
            execution_state["reasoning_summary"] = reasoning_summary
        if last_tool is not _UNSET:
            execution_state["last_tool"] = last_tool
        if last_tool_result is not _UNSET:
            execution_state["last_tool_result"] = last_tool_result
        if last_replan is not _UNSET:
            execution_state["last_replan"] = last_replan
        if last_reflection is not _UNSET:
            execution_state["last_reflection"] = last_reflection
        if last_alerts is not _UNSET:
            execution_state["last_alerts"] = list(last_alerts or [])

        execution_state["updated_at"] = datetime.now().isoformat()
        task["updated_at"] = execution_state["updated_at"]

        next_step = self._get_next_step(task)
        if next_step:
            if phase is not _UNSET:
                next_step["phase"] = execution_state.get("phase")
                next_step["recovery_mode"] = execution_state.get("recovery_mode", "none")
            if blockage_reason is not _UNSET:
                next_step["blockage_reason"] = blockage_reason
            if next_actions is not _UNSET:
                next_step["next_actions"] = self._normalize_next_actions(next_actions)
            if reasoning_summary not in (_UNSET, None, ""):
                next_step["notes"] = reasoning_summary

        return self._save_task(task)

    def get_execution_state(self) -> Optional[Dict[str, Any]]:
        task = self._load_task()
        if not task:
            return None
        return task.get("execution_state", self._default_execution_state())

    def sync_autonomous_state(
        self,
        status: Any = _UNSET,
        phase_stagnation_count: Any = _UNSET,
        repeated_action_count: Any = _UNSET,
        tool_failure_streak: Any = _UNSET,
        last_phase: Any = _UNSET,
        last_next_actions: Any = _UNSET,
        last_goal_source: Any = _UNSET,
        last_prompt_signature: Any = _UNSET,
        last_tick_at: Any = _UNSET,
        last_result: Any = _UNSET,
        stop_reason: Any = _UNSET,
    ) -> bool:
        task = self._load_task()
        if not task:
            return False

        autonomous_state = task.setdefault("autonomous_state", self._default_autonomous_state())
        if status is not _UNSET:
            autonomous_state["status"] = status
        if phase_stagnation_count is not _UNSET:
            autonomous_state["phase_stagnation_count"] = int(phase_stagnation_count or 0)
        if repeated_action_count is not _UNSET:
            autonomous_state["repeated_action_count"] = int(repeated_action_count or 0)
        if tool_failure_streak is not _UNSET:
            autonomous_state["tool_failure_streak"] = int(tool_failure_streak or 0)
        if last_phase is not _UNSET:
            autonomous_state["last_phase"] = last_phase
        if last_next_actions is not _UNSET:
            autonomous_state["last_next_actions"] = self._normalize_next_actions(last_next_actions)
        if last_goal_source is not _UNSET:
            autonomous_state["last_goal_source"] = last_goal_source
        if last_prompt_signature is not _UNSET:
            autonomous_state["last_prompt_signature"] = last_prompt_signature
        if last_tick_at is not _UNSET:
            autonomous_state["last_tick_at"] = last_tick_at
        if last_result is not _UNSET:
            autonomous_state["last_result"] = last_result
        if stop_reason is not _UNSET:
            autonomous_state["stop_reason"] = stop_reason

        autonomous_state["updated_at"] = datetime.now().isoformat()
        task["updated_at"] = autonomous_state["updated_at"]
        return self._save_task(task)

    def get_autonomous_state(self) -> Optional[Dict[str, Any]]:
        task = self._load_task()
        if not task:
            return None
        return task.get("autonomous_state", self._default_autonomous_state())

    def set_blockage(
        self,
        blockage_reason: str,
        next_actions: Optional[List[str]] = None,
        notes: Optional[str] = None,
    ) -> bool:
        return self.sync_execution_state(
            blockage_reason=blockage_reason,
            next_actions=next_actions or [],
            reasoning_summary=notes,
        )

    def complete_task(self, summary: str) -> bool:
        task = self._load_task()
        if not task:
            print("没有活跃的任务")
            return False

        task["status"] = "completed"
        task["completed_at"] = datetime.now().isoformat()
        task["summary"] = summary

        self._save_task(task)
        self._archive_task(task)

        print(f"任务已完成: {task['task_id']}")
        print(f"  描述: {task['description']}")
        print(f"  总结: {summary}")
        return True

    def abort_task(self, reason: str) -> bool:
        task = self._load_task()
        if not task:
            print("没有活跃的任务")
            return False

        task["status"] = "aborted"
        task["aborted_at"] = datetime.now().isoformat()
        task["abort_reason"] = reason

        self._save_task(task)
        self._archive_task(task)

        print(f"任务已中止: {task['task_id']}")
        print(f"  原因: {reason}")
        return True

    def check_task_alignment(self) -> Optional[Dict[str, Any]]:
        task = self._load_task()
        if not task:
            return None

        total_steps = len(task["steps"])
        completed_steps = sum(1 for step in task["steps"] if step["status"] == "completed")
        current_step_obj = self._get_next_step(task)

        alignment_status = "on_track"
        if current_step_obj is None:
            alignment_status = "completed"
        elif completed_steps == 0 and task["current_step"] == 0:
            alignment_status = "not_started"

        return {
            "task_id": task["task_id"],
            "description": task["description"],
            "objective": task["objective"],
            "alignment_status": alignment_status,
            "current_step": task["current_step"],
            "next_step": current_step_obj,
            "completed_steps": completed_steps,
            "total_steps": total_steps,
            "progress_percentage": (completed_steps / total_steps * 100) if total_steps > 0 else 0,
            "execution_state": task.get("execution_state", self._default_execution_state()),
            "autonomous_state": task.get("autonomous_state", self._default_autonomous_state()),
        }

    def get_all_archived_tasks(self) -> List[Dict[str, Any]]:
        archived_tasks = []
        for file in self.archived_dir.glob("task_*.json"):
            try:
                with open(file, "r", encoding="utf-8") as handle:
                    archived_tasks.append(self._ensure_task_shape(json.load(handle)))
            except Exception as e:
                print(f"读取归档任务失败 {file}: {e}")
        return sorted(archived_tasks, key=lambda item: item.get("created_at", ""), reverse=True)

    def clear_current_task(self) -> bool:
        try:
            if self.current_task_file.exists():
                self.current_task_file.unlink()
                print("当前任务已清除")
                return True
            return False
        except Exception as e:
            print(f"清除任务失败: {e}")
            return False
