"""
Phase 5: 交付闭环与 Git 工程协作

提供:
- GitStatusProbe: Git 状态感知（分支、脏状态、变更摘要）
- ChangeDigest: 结构化变更摘要（改了什么、为什么、风险）
- DeliveryReport: 完整交付报告生成器
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def _decode_bytes(byte_data: bytes) -> str:
    """智能解码 subprocess 输出：UTF-8 → GBK → 系统默认(replace) → latin1(replace)"""
    if not byte_data:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return byte_data.decode(encoding)
        except UnicodeDecodeError:
            continue
    try:
        return byte_data.decode(sys.getdefaultencoding(), errors="replace")
    except Exception:
        return byte_data.decode("latin1", errors="replace")


@dataclass
class GitStatus:
    branch: str = "unknown"
    dirty: bool = False
    staged: int = 0
    modified: int = 0
    untracked: int = 0
    deleted: int = 0
    ahead: int = 0
    behind: int = 0
    last_commit: str = ""
    last_commit_msg: str = ""
    files_summary: List[str] = field(default_factory=list)


class GitStatusProbe:
    """Git 状态探测器——零依赖，纯 subprocess"""

    def __init__(self, repo_path: Optional[str] = None):
        self.repo_path = Path(repo_path) if repo_path else Path.cwd()
        self.last_status: Optional[GitStatus] = None

    def probe(self) -> GitStatus:
        status = GitStatus()

        # 分支
        status.branch = self._run(["git", "branch", "--show-current"]).strip() or "unknown"

        # 脏状态统计
        short = self._run(["git", "status", "--short"])
        for line in short.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("??"):
                status.untracked += 1
            elif line.startswith("A ") or line.startswith("AM") or line.startswith("A"):
                status.staged += 1
            elif line.startswith("M ") or line.startswith(" M"):
                status.modified += 1
            elif line.startswith("D ") or line.startswith("AD"):
                status.deleted += 1
            elif " " in line[:2]:
                status.modified += 1

        status.dirty = (status.modified + status.staged + status.deleted + status.untracked) > 0

        # 领先/落后
        try:
            self._run(["git", "fetch", "--quiet"], timeout=10)
        except Exception:
            pass
        ahead_behind = self._run(["git", "rev-list", "--left-right", "--count", f"{status.branch}...origin/{status.branch}"])
        parts = ahead_behind.strip().split()
        if len(parts) == 2:
            try:
                status.ahead = int(parts[0])
                status.behind = int(parts[1])
            except ValueError:
                pass

        # 最近提交
        status.last_commit = self._run(["git", "log", "-1", "--format=%h"]).strip()
        status.last_commit_msg = self._run(["git", "log", "-1", "--format=%s"]).strip()

        # 变更文件摘要（最多 20 条）
        changed = self._run(["git", "diff", "--name-only", "HEAD"]).strip().split("\n")[:20]
        status.files_summary = [f for f in changed if f]

        self.last_status = status
        return status

    def format_status_console(self, status: Optional[GitStatus] = None) -> str:
        return self.format_console(status or self.last_status or self.probe())

    def format_console(self, status: GitStatus) -> str:
        lines = ["─" * 50, "  Git 状态", "─" * 50]
        lines.append(f"  分支: {status.branch}  |  {'脏' if status.dirty else '干净'}")
        lines.append(f"  暂存:{status.staged}  修改:{status.modified}  未跟踪:{status.untracked}  删除:{status.deleted}")
        if status.ahead or status.behind:
            lines.append(f"  领先:{status.ahead}  落后:{status.behind}")
        if status.last_commit:
            lines.append(f"  最近提交: {status.last_commit}  {status.last_commit_msg[:60]}")
        if status.files_summary:
            lines.append("  变更文件:")
            for f in status.files_summary[:8]:
                lines.append(f"    - {f}")
            if len(status.files_summary) > 8:
                lines.append(f"    ... 共 {len(status.files_summary)} 个文件")
        lines.append("─" * 50)
        return "\n".join(lines)

    def format_console(self, status: GitStatus) -> str:
        lines = ["-" * 50, "  Git status", "-" * 50]
        clean_state = "dirty" if status.dirty else "clean"
        lines.append(f"  branch: {status.branch}  |  {clean_state}")
        lines.append(
            f"  staged:{status.staged}  modified:{status.modified}  "
            f"untracked:{status.untracked}  deleted:{status.deleted}"
        )
        if status.ahead or status.behind:
            lines.append(f"  ahead:{status.ahead}  behind:{status.behind}")
        if status.last_commit:
            lines.append(f"  last commit: {status.last_commit}  {status.last_commit_msg[:60]}")
        if status.files_summary:
            lines.append("  changed files:")
            for path in status.files_summary[:8]:
                lines.append(f"    - {path}")
            if len(status.files_summary) > 8:
                lines.append(f"    ... {len(status.files_summary)} files total")
        lines.append("-" * 50)
        return "\n".join(lines)

    def _run(self, cmd: List[str], timeout: int = 10) -> str:
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.repo_path),
                capture_output=True,
                timeout=timeout,
            )
            if result.stdout:
                return _decode_bytes(result.stdout)
            return ""
        except Exception:
            return ""


@dataclass
class ChangeItem:
    path: str = ""
    change_type: str = "modified"  # added / modified / deleted
    summary: str = ""
    why: str = ""
    risk: str = "low"


@dataclass
class DeliveryReport:
    task_id: str = ""
    title: str = ""
    timestamp: str = ""
    branch: str = ""
    changes: List[ChangeItem] = field(default_factory=list)
    verification: str = ""
    risks: List[str] = field(default_factory=list)
    unfinished: List[str] = field(default_factory=list)

    def format_markdown(self) -> str:
        lines = [
            f"# 交付报告: {self.title}",
            "",
            f"- **任务**: {self.task_id}",
            f"- **时间**: {self.timestamp}",
            f"- **分支**: {self.branch}",
            "",
            "## 变更摘要",
            "",
            "| 文件 | 类型 | 说明 | 原因 | 风险 |",
            "|------|------|------|------|------|",
        ]
        for c in self.changes:
            lines.append(f"| {c.path} | {c.change_type} | {c.summary} | {c.why} | {c.risk} |")

        if self.verification:
            lines.extend(["", "## 验证方法", "", self.verification])

        if self.risks:
            lines.extend(["", "## 风险与注意事项"])
            for r in self.risks:
                lines.append(f"- ⚠️ {r}")

        if self.unfinished:
            lines.extend(["", "## 未完成项"])
            for u in self.unfinished:
                lines.append(f"- 📋 {u}")

        return "\n".join(lines)


def build_delivery_report(
    task_id: str,
    title: str,
    changes: List[Dict[str, str]],
    verification: str = "",
    risks: Optional[List[str]] = None,
    unfinished: Optional[List[str]] = None,
    branch: str = "",
) -> DeliveryReport:
    """便捷工厂——从简化参数构建交付报告"""
    probe = GitStatusProbe()
    try:
        git_status = probe.probe()
        branch = branch or git_status.branch
    except Exception:
        pass

    report = DeliveryReport(
        task_id=task_id,
        title=title,
        timestamp=datetime.now().isoformat(),
        branch=branch,
        changes=[ChangeItem(**c) for c in changes],
        verification=verification,
        risks=risks or [],
        unfinished=unfinished or [],
    )
    return report
