#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Xenon 异步下载工具 — 支持后台下载、断点续传、轮询池集成

设计原则:
  1. 异步非阻塞: 下载在后台线程执行，立即返回 task_id
  2. 断点续传: HTTP 下载支持 Range 请求，中断后可恢复
  3. 轮询池推送: 下载完成/失败时自动推送到 MessagePollingPool
  4. 状态持久化: 下载任务状态持久化到 JSON 文件，重启可恢复
  5. 终端命令支持: 支持后台执行 winget/pip 等长时间运行的安装命令

用法:
  manager = DownloadToolManager()

  # HTTP 下载
  result = manager.download("https://example.com/file.zip", "C:/downloads/file.zip")
  # → {"success": True, "data": {"task_id": "abc123", "status": "downloading"}}

  # 查询进度
  status = manager.download_status("abc123")
  # → {"success": True, "data": {"progress": 45.2, "speed": "2.3 MB/s", ...}}

  # 终端命令下载/安装
  result = manager.download_exec("winget install ffmpeg")
  # → {"success": True, "data": {"task_id": "def456", "status": "running"}}
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ── 默认配置 ──
STATE_FILE = Path("Memory/downloads.json")
DEFAULT_CHUNK_SIZE = 8192          # 8KB 下载块
DEFAULT_HTTP_TIMEOUT = 30          # HTTP 连接超时（秒）
DEFAULT_EXEC_TIMEOUT = 3600        # 终端命令默认超时（1 小时）
MAX_TASK_HISTORY = 50              # 保留最近任务数
STALL_CHECK_INTERVAL = 30         # 终端进程卡死检测间隔（秒）
STALL_THRESHOLD_CHECKS = 3        # 连续 N 次无输出 → 标记为 stalled


# ═══════════════════════════════════════════════════════════════════ #
#  DownloadToolManager
# ═══════════════════════════════════════════════════════════════════ #

class DownloadToolManager:
    """Xenon 异步下载工具管理器。

    提供 HTTP 下载（含断点续传）和终端命令后台执行能力。
    所有异步任务完成后自动推送结果到 MessagePollingPool。
    """

    def __init__(self) -> None:
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._state_file = STATE_FILE
        self._cancel_flags: Dict[str, threading.Event] = {}
        self._load_state()

    # ═══════════════════════════════════════════════════════════════ #
    #  公开 API — 下载
    # ═══════════════════════════════════════════════════════════════ #

    def download(
        self,
        url: str,
        save_path: Optional[str] = None,
        background: bool = True,
        resume: bool = True,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """HTTP/HTTPS 文件下载。

        Args:
            url: 下载链接
            save_path: 保存路径（含文件名）。为 None 或空时自动解析到系统 Downloads 目录
            background: 是否后台下载（默认 True，立即返回 task_id）
            resume: 是否启用断点续传（默认 True）
            timeout: HTTP 连接超时秒数（默认 30）

        Returns:
            后台模式: {"success": True, "data": {"task_id": "...", "status": "downloading"}}
            同步模式: 下载完成后的结果字典
        """
        # ── 参数校验 ──
        if not url or not isinstance(url, str):
            return {"success": False, "error": "url 不能为空"}

        # save_path 为空时自动解析到系统 Downloads 目录
        if not save_path:
            filename = os.path.basename(url.split("?")[0])  # 去掉 URL 查询参数
            if not filename or "." not in filename:
                filename = f"download_{uuid.uuid4().hex[:8]}"
            save_path = os.path.join(self._get_system_downloads_dir(), filename)

        if not isinstance(save_path, str):
            return {"success": False, "error": "save_path 类型错误"}

        # 确保保存目录存在
        save_dir = os.path.dirname(os.path.abspath(save_path))
        try:
            os.makedirs(save_dir, exist_ok=True)
        except OSError as e:
            return {"success": False, "error": f"无法创建保存目录 {save_dir}: {e}"}

        task_id = uuid.uuid4().hex[:8]
        http_timeout = timeout or DEFAULT_HTTP_TIMEOUT

        # 初始化任务状态
        self._register_task(task_id, {
            "type": "http_download",
            "url": url,
            "save_path": os.path.abspath(save_path),
            "status": "starting",
            "resume": resume,
            "created_at": time.time(),
        })

        if background:
            cancel_event = threading.Event()
            self._cancel_flags[task_id] = cancel_event
            thread = threading.Thread(
                target=self._do_http_download,
                args=(task_id, url, save_path, resume, http_timeout, cancel_event),
                daemon=True,
                name=f"download_{task_id}",
            )
            thread.start()
            return {
                "success": True,
                "data": {
                    "task_id": task_id,
                    "status": "downloading",
                    "message": (
                        f"下载任务已启动（task_id={task_id}），"
                        f"完成后结果将出现在轮询池中。"
                    ),
                },
            }
        else:
            # 同步模式（阻塞等待）
            cancel_event = threading.Event()
            self._cancel_flags[task_id] = cancel_event
            return self._do_http_download(
                task_id, url, save_path, resume, http_timeout, cancel_event
            )

    def download_exec(
        self,
        command: str,
        background: bool = True,
        timeout: int = DEFAULT_EXEC_TIMEOUT,
        working_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """后台执行终端下载/安装命令（如 winget install / pip install）。

        命令在后台线程中执行，不受 terminal_handler 的 300 秒超时限制。
        最长执行时间由 timeout 参数控制（默认 1 小时）。

        Args:
            command: 要执行的终端命令
            background: 是否后台执行（默认 True）
            timeout: 命令超时秒数（默认 3600）
            working_dir: 工作目录

        Returns:
            {"success": True, "data": {"task_id": "...", "status": "running"}}
        """
        if not command or not isinstance(command, str):
            return {"success": False, "error": "command 不能为空"}

        task_id = uuid.uuid4().hex[:8]

        self._register_task(task_id, {
            "type": "terminal_exec",
            "command": command,
            "working_dir": os.path.abspath(working_dir) if working_dir else os.getcwd(),
            "status": "starting",
            "created_at": time.time(),
        })

        if background:
            cancel_event = threading.Event()
            self._cancel_flags[task_id] = cancel_event
            thread = threading.Thread(
                target=self._do_exec_command,
                args=(task_id, command, timeout, working_dir, cancel_event),
                daemon=True,
                name=f"exec_{task_id}",
            )
            thread.start()
            return {
                "success": True,
                "data": {
                    "task_id": task_id,
                    "status": "running",
                    "message": (
                        f"命令已开始在后台执行（task_id={task_id}），"
                        f"最长运行 {timeout} 秒，完成后结果将出现在轮询池中。"
                    ),
                },
            }
        else:
            cancel_event = threading.Event()
            self._cancel_flags[task_id] = cancel_event
            return self._do_exec_command(
                task_id, command, timeout, working_dir, cancel_event
            )

    # ═══════════════════════════════════════════════════════════════ #
    #  公开 API — 查询 / 管理
    # ═══════════════════════════════════════════════════════════════ #

    def download_status(self, task_id: str) -> Dict[str, Any]:
        """查询下载任务的状态和进度。

        Args:
            task_id: download/download_exec 返回的任务 ID

        Returns:
            {"success": True, "data": {...}}
            如果 task_id 不存在: {"success": False, "error": "任务不存在"}

        终端命令任务额外返回:
            - is_stalled / stall_warning / idle_seconds: 卡死检测信息
            - last_output_snippet: 最近几行输出（可见进程在干什么）
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return {"success": False, "error": f"任务 {task_id} 不存在"}

            data = dict(task)
            # 添加人类可读字段
            data["age_seconds"] = round(time.time() - task["created_at"], 1)
            if data["age_seconds"] < 60:
                data["age_human"] = f"{data['age_seconds']:.0f}s"
            elif data["age_seconds"] < 3600:
                data["age_human"] = f"{data['age_seconds'] / 60:.1f}m"
            else:
                data["age_human"] = f"{data['age_seconds'] / 3600:.1f}h"

            # ── 终端命令增强信息 ──
            if task.get("type") == "terminal_exec":
                # 卡死警告
                if task.get("is_stalled"):
                    idle = task.get("idle_seconds", 0)
                    data["stall_warning"] = (
                        f"⚠️ 进程已 {idle} 秒无任何输出，可能已卡死。"
                        f"进程仍在运行但无进展，建议检查网络或手动取消任务。"
                    )
                # 当前空闲时长（距最后一次输出）
                last_out = task.get("last_output_time")
                if last_out:
                    data["idle_seconds"] = round(time.time() - last_out)
                # 部分输出快照（最后 5 行，让用户看到进程在干嘛）
                stdout = task.get("stdout", "")
                if stdout:
                    lines = [l for l in stdout.split("\n") if l.strip()]
                    snippet = lines[-5:]
                    if snippet:
                        data["last_output_snippet"] = "\n".join(snippet)

            return {"success": True, "data": data}

    def download_list(self, status_filter: Optional[str] = None) -> Dict[str, Any]:
        """列出所有下载任务。

        Args:
            status_filter: 可选的状态过滤，如 "downloading" / "completed" / "failed"

        Returns:
            {"success": True, "data": [task_summary, ...]}
        """
        with self._lock:
            tasks = list(self._tasks.values())
            if status_filter:
                tasks = [t for t in tasks if t.get("status") == status_filter]

            # 返回摘要（不包含完整输出，避免过大）
            summaries = []
            for t in tasks:
                summary = {
                    "task_id": t.get("task_id"),
                    "type": t.get("type"),
                    "status": t.get("status"),
                    "progress": t.get("progress"),
                    "speed": t.get("speed"),
                    "url": (t.get("url") or "")[:100],
                    "command": (t.get("command") or "")[:100],
                    "error": (t.get("error") or "")[:200],
                    "age_seconds": round(time.time() - t["created_at"], 1),
                }
                summaries.append(summary)

            # 按创建时间降序
            summaries.sort(key=lambda s: s.get("age_seconds", 0))

            return {"success": True, "data": summaries, "total": len(summaries)}

    def download_cancel(self, task_id: str) -> Dict[str, Any]:
        """取消正在进行的下载任务。

        Args:
            task_id: 任务 ID

        Returns:
            {"success": True, "message": "..."}
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return {"success": False, "error": f"任务 {task_id} 不存在"}

            if task.get("status") not in ("downloading", "running", "starting"):
                return {
                    "success": False,
                    "error": f"任务 {task_id} 当前状态为 {task.get('status')}，无法取消",
                }

            # 设置取消标志
            cancel_event = self._cancel_flags.get(task_id)
            if cancel_event:
                cancel_event.set()

            task["status"] = "cancelled"
            task["cancelled_at"] = time.time()
            self._save_state()

            return {"success": True, "message": f"任务 {task_id} 已标记为取消"}

    def download_wait(
        self, task_id: str, timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        """阻塞等待下载任务完成。

        Args:
            task_id: 任务 ID
            timeout: 等待超时秒数（None 表示不限）

        Returns:
            任务完成后的完整结果
        """
        start = time.time()
        while True:
            with self._lock:
                task = self._tasks.get(task_id)
                if not task:
                    return {"success": False, "error": f"任务 {task_id} 不存在"}
                status = task.get("status")

            if status in ("completed", "failed", "cancelled"):
                return {"success": True, "data": dict(task)}

            if timeout and (time.time() - start) > timeout:
                return {
                    "success": False,
                    "error": f"等待任务 {task_id} 超时（{timeout}s），当前状态: {status}",
                    "data": dict(task),
                }

            time.sleep(0.5)

    # ═══════════════════════════════════════════════════════════════ #
    #  内部 — HTTP 下载核心
    # ═══════════════════════════════════════════════════════════════ #

    def _do_http_download(
        self,
        task_id: str,
        url: str,
        save_path: str,
        resume: bool,
        http_timeout: int,
        cancel_event: threading.Event,
    ) -> Dict[str, Any]:
        """在后台线程中执行 HTTP 下载（支持断点续传）。"""
        try:
            # ── 检查断点续传 ──
            existing_size = 0
            if resume and os.path.exists(save_path):
                existing_size = os.path.getsize(save_path)
                if existing_size > 0:
                    self._update_task(task_id, {
                        "status": "downloading",
                        "resumed_from": existing_size,
                    })

            # ── 发送请求（带 Range 头） ──
            headers: Dict[str, str] = {}
            if existing_size > 0:
                headers["Range"] = f"bytes={existing_size}-"

            req = Request(url, headers=headers)
            response = urlopen(req, timeout=http_timeout)

            # ── 解析响应头，确定总大小 ──
            total_size = existing_size
            content_range = response.headers.get("Content-Range")
            if content_range and "/" in content_range:
                # 格式: "bytes 1024-2047/4096" 或 "bytes 1024-2047/*"
                total_part = content_range.split("/")[-1].strip()
                if total_part.isdigit():
                    total_size = int(total_part)

            content_length = response.headers.get("Content-Length")
            if content_length and content_length.isdigit():
                if total_size == existing_size:
                    total_size = existing_size + int(content_length)

            # 206 Partial Content → 服务器支持断点续传
            resumed = (response.status == 206)

            # ── 写入文件 ──
            mode = "ab" if existing_size > 0 else "wb"
            with open(save_path, mode) as f:
                chunk_size = DEFAULT_CHUNK_SIZE
                downloaded = existing_size
                start_time = time.time()
                last_update = start_time

                while True:
                    # 检查取消标志
                    if cancel_event.is_set():
                        self._update_task(task_id, {
                            "status": "cancelled",
                            "downloaded": downloaded,
                            "message": "下载已被取消，已下载部分保留可用于续传",
                        })
                        return {
                            "success": False,
                            "error": "下载已取消",
                            "task_id": task_id,
                            "downloaded": downloaded,
                            "save_path": save_path,
                        }

                    chunk = response.read(chunk_size)
                    if not chunk:
                        break

                    f.write(chunk)
                    downloaded += len(chunk)

                    # 每 0.5 秒更新进度（避免锁竞争）
                    now = time.time()
                    if now - last_update >= 0.5:
                        elapsed = now - start_time
                        speed = downloaded / elapsed if elapsed > 0 else 0
                        progress = (
                            round(downloaded / total_size * 100, 1)
                            if total_size > 0
                            else 0
                        )
                        eta = (
                            (total_size - downloaded) / speed
                            if speed > 0 and total_size > 0
                            else None
                        )
                        self._update_task(task_id, {
                            "status": "downloading",
                            "downloaded": downloaded,
                            "total": total_size,
                            "progress": progress,
                            "speed": self._format_speed(speed),
                            "eta": self._format_eta(eta) if eta else None,
                            "resumed": resumed,
                        })
                        last_update = now

            # ── 下载完成 ──
            elapsed = time.time() - start_time
            avg_speed = downloaded / elapsed if elapsed > 0 else 0

            result = {
                "status": "completed",
                "downloaded": downloaded,
                "total": total_size,
                "progress": 100,
                "speed": self._format_speed(avg_speed),
                "elapsed": self._format_eta(elapsed),
                "resumed": resumed,
                "completed_at": time.time(),
            }
            self._update_task(task_id, result)

            pool_result = {
                "success": True,
                "task_id": task_id,
                "file": save_path,
                "size": downloaded,
                "url": url,
                "elapsed": self._format_eta(elapsed),
                "resumed": resumed,
            }
            self._push_to_pool(task_id, pool_result)

            return {"success": True, "data": {**result, "task_id": task_id}}

        except HTTPError as e:
            return self._handle_download_error(task_id, url, save_path, f"HTTP {e.code}: {e.reason}")
        except URLError as e:
            return self._handle_download_error(task_id, url, save_path, f"网络错误: {e.reason}")
        except Exception as e:
            return self._handle_download_error(task_id, url, save_path, str(e))

    def _handle_download_error(
        self, task_id: str, url: str, save_path: str, error_msg: str
    ) -> Dict[str, Any]:
        """统一处理下载错误：更新状态 + 推送轮询池。"""
        existing_size = 0
        if os.path.exists(save_path):
            existing_size = os.path.getsize(save_path)

        error_data = {
            "status": "failed",
            "error": error_msg,
            "downloaded": existing_size,
            "failed_at": time.time(),
        }
        self._update_task(task_id, error_data)

        self._push_to_pool(task_id, {
            "success": False,
            "task_id": task_id,
            "error": error_msg,
            "url": url,
            "save_path": save_path,
            "partial_size": existing_size,
            "can_resume": existing_size > 0,
        })

        return {"success": False, "error": error_msg, "task_id": task_id,
                "partial_size": existing_size, "can_resume": existing_size > 0}

    # ═══════════════════════════════════════════════════════════════ #
    #  内部 — 终端命令执行核心
    # ═══════════════════════════════════════════════════════════════ #

    def _do_exec_command(
        self,
        task_id: str,
        command: str,
        timeout: int,
        working_dir: Optional[str],
        cancel_event: threading.Event,
    ) -> Dict[str, Any]:
        """在后台线程中执行终端命令（带卡死检测和实时输出收集）。"""
        process = None
        try:
            exec_dir = (
                os.path.abspath(working_dir)
                if working_dir
                else os.getcwd()
            )

            self._update_task(task_id, {"status": "running"})

            popen_kwargs: Dict[str, Any] = {
                "shell": True,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "stdin": subprocess.PIPE,
                "cwd": exec_dir,
                "env": {**os.environ, "PYTHONIOENCODING": "utf-8"},
                "bufsize": 0,  # 无缓冲，支持实时逐行读取
            }

            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

            process = subprocess.Popen(command, **popen_kwargs)
            # 关闭 stdin，防止子进程等待交互输入导致假死
            try:
                process.stdin.close()
            except Exception:
                pass

            start_time = time.time()

            # ── 启动 stdout/stderr 实时读取线程 ──
            # 独立线程持续收集输出，主循环通过行数变化检测进程是否在干活
            stdout_lines: List[str] = []
            stderr_lines: List[str] = []

            def _read_stream(stream, lines_list: List[str], encoding_hint: str = "utf-8"):
                """逐行读取子进程输出流，捕获解码异常。"""
                try:
                    for raw_line in iter(stream.readline, b""):
                        try:
                            lines_list.append(raw_line.decode(encoding_hint))
                        except UnicodeDecodeError:
                            lines_list.append(raw_line.decode(encoding_hint, errors="replace"))
                except (ValueError, OSError):
                    pass
                finally:
                    try:
                        stream.close()
                    except Exception:
                        pass

            t_stdout = threading.Thread(
                target=_read_stream,
                args=(process.stdout, stdout_lines, "utf-8"),
                daemon=True,
                name=f"stdout_{task_id}",
            )
            t_stderr = threading.Thread(
                target=_read_stream,
                args=(process.stderr, stderr_lines, "gbk"),
                daemon=True,
                name=f"stderr_{task_id}",
            )
            t_stdout.start()
            t_stderr.start()

            # ── 轮询等待（卡死检测 + 取消检测）──
            poll_interval = 0.5
            stall_check_interval = STALL_CHECK_INTERVAL
            stall_threshold = STALL_THRESHOLD_CHECKS
            elapsed = 0.0
            last_stdout_lines = 0
            last_stderr_lines = 0
            last_progress_time = start_time
            last_stall_check = start_time
            stall_count = 0
            returncode = None

            while elapsed < timeout:
                try:
                    returncode = process.wait(timeout=poll_interval)
                    break  # 进程正常退出
                except subprocess.TimeoutExpired:
                    elapsed += poll_interval

                    # ── ① 取消检测 ──
                    if cancel_event.is_set():
                        self._terminate_process(process)
                        t_stdout.join(timeout=2)
                        t_stderr.join(timeout=2)
                        self._update_task(task_id, {
                            "status": "cancelled",
                            "message": "命令已被取消",
                            "cancelled_at": time.time(),
                            "stdout_lines": len(stdout_lines),
                            "stderr_lines": len(stderr_lines),
                        })
                        return {
                            "success": False,
                            "error": "命令已被取消",
                            "task_id": task_id,
                        }

                    # ── ② 卡死检测：输出行数是否增长 ──
                    now = time.time()
                    current_stdout = len(stdout_lines)
                    current_stderr = len(stderr_lines)
                    output_changed = (
                        current_stdout != last_stdout_lines
                        or current_stderr != last_stderr_lines
                    )

                    if output_changed:
                        # 有新输出 → 进程活着，重置卡死计数
                        last_progress_time = now
                        last_stdout_lines = current_stdout
                        last_stderr_lines = current_stderr
                        if stall_count > 0:
                            stall_count = 0
                            self._update_task(task_id, {
                                "last_output_time": now,
                                "stdout_lines": current_stdout,
                                "is_stalled": False,
                                "stall_count": 0,
                            })
                    elif now - last_stall_check >= stall_check_interval:
                        # 每 STALL_CHECK_INTERVAL 秒评估一次
                        last_stall_check = now
                        if process.poll() is None:  # 进程还活着但无输出
                            stall_count += 1
                            idle_seconds = round(now - last_progress_time)
                            stall_info: Dict[str, Any] = {
                                "stall_count": stall_count,
                                "last_output_time": last_progress_time,
                                "stdout_lines": current_stdout,
                                "idle_seconds": idle_seconds,
                            }
                            if stall_count >= stall_threshold:
                                # 连续 STALL_THRESHOLD_CHECKS × STALL_CHECK_INTERVAL 秒无输出
                                stall_info["is_stalled"] = True
                                stall_info["message"] = (
                                    f"⚠️ 进程已 {idle_seconds} 秒无任何输出，可能已卡死。"
                                    f"进程仍在运行但无进展，建议检查网络或手动取消任务。"
                                )
                            self._update_task(task_id, stall_info)

            # ── 等待读取线程收尾 ──
            t_stdout.join(timeout=5)
            t_stderr.join(timeout=5)

            # ── 超时处理 ──
            if returncode is None:
                self._terminate_process(process)
                t_stdout.join(timeout=2)
                t_stderr.join(timeout=2)

                stdout_text = "".join(stdout_lines)
                stderr_text = "".join(stderr_lines)
                error_msg = f"命令执行超时（{timeout}s）"
                self._update_task(task_id, {
                    "status": "failed",
                    "error": error_msg,
                    "stdout": stdout_text[:5000],
                    "stderr": stderr_text[:2000],
                    "stdout_lines": len(stdout_lines),
                    "failed_at": time.time(),
                })
                self._push_to_pool(task_id, {
                    "success": False,
                    "task_id": task_id,
                    "error": error_msg,
                    "command": command,
                })
                return {"success": False, "error": error_msg, "task_id": task_id}

            # ── 收集最终输出 ──
            stdout_text = "".join(stdout_lines)
            stderr_text = "".join(stderr_lines)
            elapsed = time.time() - start_time

            success = returncode == 0
            status = "completed" if success else "failed"

            result = {
                "status": status,
                "returncode": returncode,
                "stdout": stdout_text[:10000],
                "stderr": stderr_text[:5000],
                "stdout_lines": len(stdout_lines),
                "stderr_lines": len(stderr_lines),
                "elapsed": self._format_eta(elapsed),
                "elapsed_seconds": round(elapsed, 1),
            }

            if not success:
                result["error"] = f"命令返回码 {returncode}"

            self._update_task(task_id, {
                **result,
                "completed_at": time.time(),
                "is_stalled": False,
            })

            self._push_to_pool(task_id, {
                "success": success,
                "task_id": task_id,
                "command": command,
                "returncode": returncode,
                "elapsed": self._format_eta(elapsed),
                "summary": (stdout_text[:300] + "..." if len(stdout_text) > 300 else stdout_text),
                "error": stderr_text[:300] if stderr_text else None,
            })

            return {"success": success, "data": {**result, "task_id": task_id}}

        except Exception as e:
            error_msg = str(e)
            self._update_task(task_id, {
                "status": "failed",
                "error": error_msg,
                "failed_at": time.time(),
            })
            self._push_to_pool(task_id, {
                "success": False,
                "task_id": task_id,
                "error": error_msg,
                "command": command,
            })
            return {"success": False, "error": error_msg, "task_id": task_id}
        finally:
            # 确保异常路径下进程也被清理
            if process is not None and process.poll() is None:
                try:
                    self._terminate_process(process)
                except Exception:
                    pass

    # ═══════════════════════════════════════════════════════════════ #
    #  内部 — 轮询池推送
    # ═══════════════════════════════════════════════════════════════ #

    def _push_to_pool(self, task_id: str, result: Dict[str, Any]) -> None:
        """将任务结果推送到消息轮询池。"""
        try:
            from xenon_core.polling_pool import get_pool, PoolMessage

            pool = get_pool()
            is_success = result.get("success", False)
            pool.push(
                PoolMessage(
                    source="download_handler",
                    scenario="download",
                    msg_type="result",
                    payload={"task_id": task_id, **result},
                    priority=2 if not is_success else 1,
                    ttl=3600,  # 1 小时后过期
                )
            )
        except ImportError:
            pass  # 轮询池未初始化（如在独立脚本中运行）
        except Exception:
            pass  # 推送失败非致命

    # ═══════════════════════════════════════════════════════════════ #
    #  内部 — 任务状态管理
    # ═══════════════════════════════════════════════════════════════ #

    def _register_task(self, task_id: str, task_data: Dict[str, Any]) -> None:
        """注册新任务。"""
        with self._lock:
            task_data["task_id"] = task_id
            self._tasks[task_id] = task_data
            self._trim_history()
            self._save_state()

    def _update_task(self, task_id: str, updates: Dict[str, Any]) -> None:
        """更新任务状态。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.update(updates)
                task["updated_at"] = time.time()
            self._save_state()

    def _trim_history(self) -> None:
        """超出上限时清理最旧的任务。"""
        if len(self._tasks) <= MAX_TASK_HISTORY:
            return
        # 保留最新的任务，按创建时间排序
        sorted_tasks = sorted(
            self._tasks.items(),
            key=lambda kv: kv[1].get("created_at", 0),
            reverse=True,
        )
        keep_ids = {tid for tid, _ in sorted_tasks[:MAX_TASK_HISTORY]}
        self._tasks = {tid: t for tid, t in self._tasks.items() if tid in keep_ids}

    # ═══════════════════════════════════════════════════════════════ #
    #  内部 — 持久化
    # ═══════════════════════════════════════════════════════════════ #

    def _save_state(self) -> None:
        """将任务状态持久化到 JSON 文件。"""
        try:
            state_dir = self._state_file.parent
            state_dir.mkdir(parents=True, exist_ok=True)
            temp = state_dir / "downloads.tmp"
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(self._tasks, f, ensure_ascii=False, indent=2)
            temp.replace(self._state_file)
        except Exception:
            pass

    def _load_state(self) -> None:
        """从 JSON 文件恢复任务状态。"""
        if not self._state_file.exists():
            return
        try:
            raw = self._state_file.read_text(encoding="utf-8")
            if not raw.strip():
                return
            data = json.loads(raw)
            if isinstance(data, dict):
                # 将 running/downloading 状态的任务重置为 "interrupted"（进程已丢失）
                for tid, task in data.items():
                    if task.get("status") in ("downloading", "running", "starting"):
                        task["status"] = "interrupted"
                        task["interrupted_at"] = time.time()
                        task["message"] = "任务在进程重启时被中断，已下载部分保留可用于续传"
                self._tasks = data
        except (json.JSONDecodeError, OSError):
            pass

    # ═══════════════════════════════════════════════════════════════ #
    #  内部 — 工具函数
    # ═══════════════════════════════════════════════════════════════ #

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        """尽力终止进程树。"""
        if process is None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                import signal
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    @staticmethod
    def _safe_decode(byte_data: bytes) -> str:
        """智能解码字节输出。"""
        if not byte_data:
            return ""
        for encoding in ("utf-8", "gbk", "latin1"):
            try:
                return byte_data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return byte_data.decode("utf-8", errors="replace")

    @staticmethod
    def _format_speed(bytes_per_sec: float) -> str:
        """格式化下载速度。"""
        if bytes_per_sec >= 1024 * 1024:
            return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
        elif bytes_per_sec >= 1024:
            return f"{bytes_per_sec / 1024:.1f} KB/s"
        else:
            return f"{bytes_per_sec:.0f} B/s"

    @staticmethod
    def _format_eta(seconds: Optional[float]) -> Optional[str]:
        """格式化预计剩余时间/耗时。"""
        if seconds is None:
            return None
        if seconds >= 3600:
            return f"{seconds / 3600:.1f}h"
        elif seconds >= 60:
            return f"{seconds / 60:.1f}m"
        else:
            return f"{seconds:.0f}s"

    @staticmethod
    def _get_system_downloads_dir() -> str:
        """获取当前用户的系统 Downloads 目录路径。"""
        home = Path.home()
        candidates = [
            home / "Downloads",
            home / "下载",
        ]
        for path in candidates:
            if path.exists():
                return str(path)
        # 都没有就 fallback 到用户目录下的 Downloads
        return str(home / "Downloads")
