"""
Phase 4: 统一执行上下文与沙箱隔离

提供:
- SandboxContext: 统一管理工具执行边界（工作目录隔离、任务追踪、恢复检查点）
- ToolHealthChecker: 工具依赖健康检查（playwright、终端、网络等）
- TaskRecoveryPoint: 长任务恢复入口

设计原则:
- 向后兼容：所有参数均为可选，不影响现有调用链
- 渐进接入：各 handler 逐步接入，不强制一次性改造
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


class TaskRecoveryPoint:
    """长任务恢复检查点"""

    def __init__(
        self,
        task_id: str,
        phase: str,
        step_index: int,
        context_snapshot: Dict[str, Any],
    ) -> None:
        self.task_id = task_id
        self.phase = phase
        self.step_index = step_index
        self.timestamp = datetime.now()
        self.context_snapshot = context_snapshot

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "phase": self.phase,
            "step_index": self.step_index,
            "timestamp": self.timestamp.isoformat(),
            "context_snapshot": self.context_snapshot,
        }


class SandboxContext:
    """统一执行上下文 - 管理工具执行的工作目录边界和状态"""

    def __init__(
        self,
        workspace_root: Optional[str] = None,
        sandbox_subdir: Optional[str] = None,
    ) -> None:
        # 项目根目录
        if workspace_root:
            self.workspace_root = Path(workspace_root).resolve()
        else:
            self.workspace_root = Path.cwd()

        # 沙箱目录：默认为 workspace_root（即直接操作项目目录，向后兼容）
        if sandbox_subdir:
            self.sandbox_dir = self.workspace_root / sandbox_subdir
        else:
            self.sandbox_dir = self.workspace_root

        self.sandbox_dir.mkdir(parents=True, exist_ok=True)

        # 活跃任务追踪: task_id -> {phase, start_time, ...}
        self.active_tasks: Dict[str, Dict[str, Any]] = {}

        # 恢复检查点栈
        self.checkpoints: List[TaskRecoveryPoint] = []

        # 是否启用沙箱隔离（默认关闭 = 向后兼容）
        self.isolation_enabled = sandbox_subdir is not None

        # 工具执行统计
        self.stats: Dict[str, Any] = {
            "total_calls": 0,
            "success_calls": 0,
            "failure_calls": 0,
            "last_call_at": None,
            "last_call_tool": None,
        }

    # ── 沙箱管理 ────────────────────────────

    def enter_sandbox(self, subdir_name: str) -> Path:
        """进入隔离沙箱子目录，返回沙箱路径"""
        new_sandbox = self.workspace_root / subdir_name
        new_sandbox.mkdir(parents=True, exist_ok=True)
        self.sandbox_dir = new_sandbox
        self.isolation_enabled = True
        return self.sandbox_dir

    def leave_sandbox(self) -> None:
        """退出沙箱，回到项目根目录"""
        self.sandbox_dir = self.workspace_root
        self.isolation_enabled = False

    def create_isolated_workspace(self, task_id: str) -> Path:
        """为特定任务创建隔离工作目录: work/sandbox_{task_id}/"""
        sandbox_path = self.workspace_root / "work" / f"sandbox_{task_id}"
        sandbox_path.mkdir(parents=True, exist_ok=True)
        self.sandbox_dir = sandbox_path
        self.isolation_enabled = True
        self.active_tasks[task_id] = {
            "sandbox": str(sandbox_path),
            "start_time": datetime.now().isoformat(),
            "phase": "isolated",
        }
        return sandbox_path

    def cleanup_isolated_workspace(self, task_id: str) -> bool:
        """清理指定任务的隔离工作目录"""
        sandbox_path = self.workspace_root / "work" / f"sandbox_{task_id}"
        if sandbox_path.exists():
            shutil.rmtree(sandbox_path, ignore_errors=True)
            self.active_tasks.pop(task_id, None)
            return True
        return False

    # ── 恢复检查点 ──────────────────────────

    def save_checkpoint(
        self,
        task_id: str,
        phase: str,
        step_index: int,
        context_snapshot: Dict[str, Any],
    ) -> TaskRecoveryPoint:
        """保存恢复检查点"""
        cp = TaskRecoveryPoint(task_id, phase, step_index, context_snapshot)
        self.checkpoints.append(cp)
        # 只保留最近 20 个检查点
        if len(self.checkpoints) > 20:
            self.checkpoints = self.checkpoints[-20:]
        return cp

    def get_latest_checkpoint(
        self, task_id: Optional[str] = None
    ) -> Optional[TaskRecoveryPoint]:
        """获取最近的恢复检查点，可按 task_id 过滤"""
        if task_id:
            matching = [cp for cp in self.checkpoints if cp.task_id == task_id]
            return matching[-1] if matching else None
        return self.checkpoints[-1] if self.checkpoints else None

    def restore_checkpoint(
        self, task_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """恢复到最近检查点，返回 context_snapshot"""
        cp = self.get_latest_checkpoint(task_id)
        if cp:
            return cp.context_snapshot
        return None

    # ── 统计追踪 ────────────────────────────

    def record_call(self, tool_name: str, success: bool) -> None:
        """记录一次工具调用"""
        self.stats["total_calls"] += 1
        if success:
            self.stats["success_calls"] += 1
        else:
            self.stats["failure_calls"] += 1
        self.stats["last_call_at"] = datetime.now().isoformat()
        self.stats["last_call_tool"] = tool_name

    def get_stats(self) -> Dict[str, Any]:
        """获取执行统计"""
        return dict(self.stats)

    def resolve_path(self, file_path: str) -> Path:
        """在沙箱上下文中解析文件路径"""
        input_path = Path(file_path)
        if input_path.is_absolute():
            return input_path
        return (self.sandbox_dir / input_path).resolve()

    def is_within_sandbox(self, path: Path) -> bool:
        """检查路径是否在沙箱范围内"""
        try:
            path.resolve().relative_to(self.sandbox_dir)
            return True
        except ValueError:
            return False


class ToolHealthChecker:
    """工具依赖健康检查 - 在启动时检测关键依赖状态"""

    def __init__(self) -> None:
        self.checks: Dict[str, Dict[str, Any]] = {}

    def check_all(self) -> Dict[str, Any]:
        """运行所有健康检查，返回完整报告"""
        self.checks = {}
        self._check_python()
        self._check_playwright()
        self._check_terminal()
        self._check_network()
        return self.build_report()

    def _check_python(self) -> None:
        import sys

        self.checks["python"] = {
            "status": "ok",
            "version": sys.version,
            "executable": sys.executable,
            "in_venv": sys.prefix != sys.base_prefix,
        }

    def _check_playwright(self) -> None:
        try:
            import playwright

            self.checks["playwright"] = {
                "status": "ok",
                "version": getattr(playwright, "__version__", "unknown"),
            }
        except ImportError:
            self.checks["playwright"] = {
                "status": "unavailable",
                "error": "playwright 未安装。浏览器自动化功能不可用。",
                "fix_hint": "运行: pip install playwright && playwright install",
            }

    def _check_terminal(self) -> None:
        import platform
        import subprocess

        system = platform.system()
        self.checks["terminal"] = {
            "status": "ok",
            "system": system,
        }

        # 检查是否有可用的 shell
        if system == "Windows":
            try:
                subprocess.run(
                    ["cmd", "/c", "echo", "test"],
                    capture_output=True,
                    timeout=5,
                )
                self.checks["terminal"]["shell"] = "cmd.exe"
            except Exception:
                self.checks["terminal"]["shell"] = "unknown"
        else:
            for shell in ["/bin/bash", "/bin/sh"]:
                if Path(shell).exists():
                    self.checks["terminal"]["shell"] = shell
                    break
            else:
                self.checks["terminal"]["shell"] = "unknown"

    def _check_network(self) -> None:
        import socket

        try:
            socket.create_connection(("8.8.8.8", 53), timeout=3)
            self.checks["network"] = {"status": "ok", "connectivity": True}
        except OSError:
            self.checks["network"] = {
                "status": "degraded",
                "connectivity": False,
                "warning": "无法连接外部网络，web_search 等工具可能不可用",
            }

    def build_report(self) -> Dict[str, Any]:
        """构建结构化健康报告"""
        ok_count = sum(1 for c in self.checks.values() if c.get("status") == "ok")
        total = len(self.checks)
        return {
            "timestamp": datetime.now().isoformat(),
            "summary": f"{ok_count}/{total} 检查通过",
            "all_ok": ok_count == total,
            "details": dict(self.checks),
        }

    def format_report_console(self) -> str:
        """生成适合控制台输出的健康报告"""
        lines = []
        lines.append("-" * 50)
        lines.append("  工具健康检查报告")
        lines.append("-" * 50)
        for name, check in self.checks.items():
            status = check.get("status", "unknown")
            label = "OK" if status == "ok" else "WARN" if status == "degraded" else "FAIL"
            lines.append(f"  [{label}] {name}: {status}")
            if "error" in check:
                lines.append(f"      - {check['error']}")
            if "fix_hint" in check:
                lines.append(f"      - fix: {check['fix_hint']}")
        lines.append("-" * 50)
        report = self.build_report()
        lines.append(f"  结果: {report['summary']}")
        lines.append("-" * 50)
        return "\n".join(lines)


# ── 便利工厂函数 ────────────────────────────


def create_default_context(workspace_root: Optional[str] = None) -> SandboxContext:
    """创建默认沙箱上下文（不启用隔离，向后兼容）"""
    return SandboxContext(workspace_root=workspace_root)


def create_isolated_context(
    workspace_root: str, task_id: str
) -> SandboxContext:
    """创建隔离沙箱上下文"""
    ctx = SandboxContext(workspace_root=workspace_root)
    ctx.create_isolated_workspace(task_id)
    return ctx
