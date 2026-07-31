"""
Xenon 系统级心跳机制

提供进程健康监控、工具执行统计和看门狗支持。

组件:
    HeartbeatStats:     线程安全的统计收集器（工具成功率、响应时间等）
    HeartbeatManager:   后台心跳线程，周期性写入心跳文件
    start_heartbeat():  便捷启动函数
    stop_heartbeat():   便捷停止函数

心跳文件:
    logs/heartbeat.json          — 当前心跳状态（覆盖写入）
    logs/heartbeat_history.jsonl — 心跳历史记录（追加写入，供分析）

环境变量:
    XENON_HEARTBEAT_INTERVAL:       心跳间隔秒数，默认 5
    XENON_HEARTBEAT_STALE_TIMEOUT:  判定死亡超时秒数（给看门狗用），默认 30
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── 默认配置 ──
DEFAULT_HEARTBEAT_INTERVAL = int(os.environ.get("XENON_HEARTBEAT_INTERVAL", "5"))
DEFAULT_STALE_TIMEOUT = int(os.environ.get("XENON_HEARTBEAT_STALE_TIMEOUT", "30"))
HEARTBEAT_FILE_NAME = "heartbeat.json"
HEARTBEAT_HISTORY_FILE_NAME = "heartbeat_history.jsonl"

# ── 历史文件清理配置（可通过环境变量覆盖）──
MAX_HISTORY_LINES = int(os.environ.get("XENON_HEARTBEAT_HISTORY_MAX_LINES", "10000"))
HISTORY_KEEP_LINES = int(os.environ.get("XENON_HEARTBEAT_HISTORY_KEEP_LINES", "5000"))


# ── 全局统计单例 ──
_stats: Optional[HeartbeatStats] = None
_stats_lock = threading.Lock()


def _get_stats() -> "HeartbeatStats":
    """获取全局 HeartbeatStats 单例（惰性初始化）"""
    global _stats
    if _stats is None:
        with _stats_lock:
            if _stats is None:  # double-check
                _stats = HeartbeatStats()
    return _stats


# ═══════════════════════════════════════════════════════════════════ #
#  HeartbeatStats — 线程安全的统计收集器
# ═══════════════════════════════════════════════════════════════════ #

class HeartbeatStats:
    """收集工具执行和运行时统计，供心跳载荷使用。

    线程安全：所有写操作由内部锁保护。
    可通过模块级函数便捷访问：
        record_tool_success() / record_tool_failure() / record_response_time()
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total_tool_calls: int = 0
        self._failed_tool_calls: int = 0
        self._response_times_ms: List[float] = []  # 最近 N 次响应时间
        self._max_response_samples: int = 50
        self._last_error: Optional[str] = None
        self._last_error_time: Optional[str] = None
        self._start_time: float = time.time()
        self._context_utilization: float = 0.0  # 由外部设置

    # ── 写入 ──

    def record_success(self) -> None:
        with self._lock:
            self._total_tool_calls += 1

    def record_failure(self, error: str = "") -> None:
        with self._lock:
            self._total_tool_calls += 1
            self._failed_tool_calls += 1
            if error:
                self._last_error = str(error)[:500]
                self._last_error_time = datetime.now().isoformat()

    def record_response_time(self, ms: float) -> None:
        with self._lock:
            self._response_times_ms.append(ms)
            if len(self._response_times_ms) > self._max_response_samples:
                self._response_times_ms = self._response_times_ms[-self._max_response_samples:]

    def set_context_utilization(self, ratio: float) -> None:
        """设置上下文窗口利用率 (0.0 ~ 1.0)"""
        with self._lock:
            self._context_utilization = max(0.0, min(1.0, ratio))

    def set_last_error(self, error: str) -> None:
        with self._lock:
            self._last_error = str(error)[:500]
            self._last_error_time = datetime.now().isoformat()

    # ── 快照 ──

    def snapshot(self) -> Dict[str, Any]:
        """获取当前统计快照（线程安全）"""
        with self._lock:
            total = self._total_tool_calls
            failed = self._failed_tool_calls
            success_rate = (total - failed) / total if total > 0 else 1.0
            response_times = list(self._response_times_ms)
            avg_response = sum(response_times) / len(response_times) if response_times else 0.0
            uptime = time.time() - self._start_time

            return {
                "uptime_seconds": round(uptime, 1),
                "total_tool_calls": total,
                "failed_tool_calls": failed,
                "tool_success_rate": round(success_rate, 4),
                "avg_response_ms": round(avg_response, 1),
                "context_utilization": round(self._context_utilization, 4),
                "last_error": self._last_error,
                "last_error_time": self._last_error_time,
            }


# ═══════════════════════════════════════════════════════════════════ #
#  模块级便捷函数（供 tool_runtime 等调用）
# ═══════════════════════════════════════════════════════════════════ #

def record_tool_success() -> None:
    """记录一次成功的工具调用"""
    _get_stats().record_success()


def record_tool_failure(error: str = "") -> None:
    """记录一次失败的工具调用"""
    _get_stats().record_failure(error)


def record_response_time(ms: float) -> None:
    """记录一次响应耗时（毫秒）"""
    _get_stats().record_response_time(ms)


def update_context_utilization(ratio: float) -> None:
    """更新上下文利用率"""
    _get_stats().set_context_utilization(ratio)


# ═══════════════════════════════════════════════════════════════════ #
#  HeartbeatManager — 后台心跳线程
# ═══════════════════════════════════════════════════════════════════ #

class HeartbeatManager:
    """管理心跳线程的生命周期。

    用法:
        hb = HeartbeatManager(project_root)
        hb.start()   # 启动后台心跳
        ...
        hb.stop()    # 优雅停止
    """

    def __init__(
        self,
        project_root: Path,
        interval: int = DEFAULT_HEARTBEAT_INTERVAL,
        mode: str = "cli",
    ) -> None:
        self._project_root = Path(project_root)
        self._interval = interval
        self._mode = mode  # "webui" 或 "cli"
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._log_dir = self._project_root / "logs"
        self._heartbeat_file = self._log_dir / HEARTBEAT_FILE_NAME
        self._history_file = self._log_dir / HEARTBEAT_HISTORY_FILE_NAME

    # ── 内存获取 ──

    @staticmethod
    def _get_memory_mb() -> Optional[float]:
        """获取当前进程内存使用量 (MB)，依赖 psutil"""
        try:
            import psutil
            proc = psutil.Process(os.getpid())
            mem_info = proc.memory_info()
            return round(mem_info.rss / (1024 * 1024), 1)
        except Exception:
            return None

    # ── 构建心跳载荷 ──

    def _build_payload(self) -> Dict[str, Any]:
        stats = _get_stats().snapshot()
        payload: Dict[str, Any] = {
            "pid": os.getpid(),
            "timestamp": datetime.now().isoformat(),
            "mode": self._mode,
            "python_version": sys.version.split()[0],
        }
        payload.update(stats)

        mem = self._get_memory_mb()
        if mem is not None:
            payload["memory_mb"] = mem

        return payload

    # ── 写入 ──

    def _write_heartbeat(self, payload: Dict[str, Any]) -> None:
        self._log_dir.mkdir(exist_ok=True)

        # 覆盖写入当前心跳
        try:
            temp_file = self._heartbeat_file.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            temp_file.replace(self._heartbeat_file)  # 原子替换
        except OSError:
            pass  # 心跳非关键

        # 追加写入历史
        try:
            with open(self._history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass

        # 定期清理历史文件（非关键路径）
        try:
            self._rotate_history_if_needed()
        except Exception:
            pass

    # ── 历史文件轮换 ──

    def _rotate_history_if_needed(self) -> None:
        """当历史文件行数超过 MAX_HISTORY_LINES 时，保留最近 HISTORY_KEEP_LINES 条。

        采用简单粗暴的尾部截取策略：读全部行 → 取尾部 N 行 → 覆写。
        对于心跳日志这种轻量场景足够；若未来数据量极大（>100MB），
        可改为 seek + 反向读取优化。
        """
        if not self._history_file.exists():
            return

        # 快速检查：用 os.path.getsize 判断是否需要清理
        try:
            file_size = self._history_file.stat().st_size
            # 估算：每行约 300 字节，MAX_HISTORY_LINES * 300 ≈ 3MB
            # 小于 1MB 直接跳过，避免无谓读取
            if file_size < 1_000_000:
                return
        except OSError:
            return

        try:
            with open(self._history_file, "r", encoding="utf-8") as f:
                lines = f.readlines()

            if len(lines) <= MAX_HISTORY_LINES:
                return

            # 保留最近的 HISTORY_KEEP_LINES 条
            kept = lines[-HISTORY_KEEP_LINES:]
            temp_file = self._history_file.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                f.writelines(kept)
            temp_file.replace(self._history_file)
        except OSError:
            pass  # 清理失败不影响心跳

    # ── 后台循环 ──

    def _loop(self) -> None:
        while not self._stop_event.wait(timeout=self._interval):
            try:
                payload = self._build_payload()
                self._write_heartbeat(payload)

                # 轮询池 tick：所有注册的监控项在此被驱动
                try:
                    from xenon_core.polling_pool import get_pool

                    pool = get_pool(self._project_root)
                    pool.tick()
                except Exception:
                    pass  # 轮询非关键，不影响心跳
            except Exception:
                # 心跳异常不应传播，静默吞下
                pass

    # ── 公开接口 ──

    def start(self) -> None:
        """启动后台心跳线程"""
        if self._thread is not None:
            return  # 已在运行

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="xenon-heartbeat",
            daemon=True,
        )
        self._thread.start()

        # 立即写入第一跳（不等 interval）
        try:
            payload = self._build_payload()
            self._write_heartbeat(payload)
        except Exception:
            pass

    def stop(self, timeout: float = 2.0) -> None:
        """停止后台心跳线程。

        注意：心跳线程为 daemon=True，主线程退出时自动终止。
        join() 仅做轻量等待，不会阻塞 shutdown。
        """
        if self._thread is None:
            return

        self._stop_event.set()
        # join 仅等一轮心跳间隔（默认 5s），但 daemon 线程无需强等
        # 设为 timeout=0 避免阻塞 shutdown 流程
        self._thread.join(timeout=0)
        self._thread = None

        # 写入最后一跳（"stopped" 标记）
        try:
            payload = self._build_payload()
            payload["status"] = "stopped"
            self._write_heartbeat(payload)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════ #
#  全局管理器实例 & 便捷启停
# ═══════════════════════════════════════════════════════════════════ #

_manager: Optional[HeartbeatManager] = None
_manager_lock = threading.Lock()


def start_heartbeat(
    project_root: Optional[Path] = None,
    interval: int = DEFAULT_HEARTBEAT_INTERVAL,
    mode: str = "cli",
) -> HeartbeatManager:
    """启动系统级心跳（便捷函数，全局单例）

    Args:
        project_root: 项目根目录，默认通过 Xenon.py 定位
        interval: 心跳间隔秒数
        mode: "webui" 或 "cli"

    Returns:
        HeartbeatManager 实例
    """
    global _manager

    if project_root is None:
        # 尝试从当前工作目录或模块位置推断
        project_root = _infer_project_root()

    with _manager_lock:
        if _manager is not None:
            return _manager  # 已启动
        _manager = HeartbeatManager(
            project_root=project_root,
            interval=interval,
            mode=mode,
        )

        _manager.start()
    return _manager


def stop_heartbeat() -> None:
    """停止系统级心跳"""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.stop()
            _manager = None


def _infer_project_root() -> Path:
    """推断项目根目录"""
    # 从当前工作目录尝试
    cwd = Path.cwd()
    for candidate in [cwd, cwd.parent]:
        if (candidate / "Xenon.py").exists():
            return candidate
    # 兜底
    return cwd
