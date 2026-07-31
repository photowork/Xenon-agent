#!/usr/bin/env python3
"""
Xenon 定时器管理程序 v2.0
- 独立桌面 GUI (Tkinter)
- 通过 WebUI POST /chat/{session_id} 向智能体注入消息
- 每个定时器对应 WebUI 中一个独立会话窗口
- 支持间隔模式和定时模式
"""

import json
import os
import sys
import time
import uuid
import threading
import traceback
from datetime import datetime, timedelta
from typing import Optional

import requests
import tkinter as tk
from tkinter import ttk, messagebox

# ============================================================
# 配置
# ============================================================

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
WEBUI_BASE_URL = "http://127.0.0.1:8000"

# ============================================================
# 配置管理
# ============================================================

DEFAULT_CONFIG = {
    "webui_url": WEBUI_BASE_URL,
    "timers": {}  # {timer_id: {...}}
}


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # v1 → v2 迁移: timers 从 list 变为 dict
            if isinstance(cfg.get("timers"), list):
                old_timers = cfg["timers"]
                new_timers = {}
                for t in old_timers:
                    tid = t.get("id", uuid.uuid4().hex[:12])
                    new_timers[tid] = {
                        "name": t.get("name", tid),
                        "mode": t.get("mode", "interval"),
                        "task": t.get("task", ""),
                        "session_id": t.get("session_id"),
                        "enabled": t.get("enabled", False),
                        "last_triggered": None,
                    }
                    if t.get("mode") == "interval":
                        val = t.get("interval_value", 30)
                        unit = t.get("interval_unit", "minutes")
                        new_timers[tid]["interval_seconds"] = val * 60 if unit == "minutes" else val * 3600
                    else:
                        new_timers[tid]["scheduled_time"] = t.get("scheduled_time", "08:00")
                        new_timers[tid]["weekdays_only"] = bool(t.get("scheduled_days"))
                cfg["timers"] = new_timers
            # 补齐缺省字段
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            return DEFAULT_CONFIG.copy()
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ============================================================
# WebUI API 封装
# ============================================================

class WebUIClient:
    """封装与 WebUI 后端的 HTTP 通信"""

    def __init__(self, base_url: str = WEBUI_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.timeout = 10

    def create_session(self, title: str) -> Optional[str]:
        """创建新会话，返回 session_id"""
        try:
            resp = requests.post(
                f"{self.base_url}/sessions",
                json={"title": title, "seed_message": title},
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("session_id")
            return None
        except Exception as e:
            print(f"[API] 创建会话失败: {e}")
            return None

    def send_message(self, session_id: str, message: str) -> bool:
        """向指定会话发送消息。

        关键设计：必须等待 SSE 流完整结束（收到 'done' 事件），
        确保 event_generator 完成 → db.save_session_state 执行 → 消息持久化。
        半途关闭连接会导致消息"已处理但未保存"，前端窗口空白。
        """
        try:
            resp = requests.post(
                f"{self.base_url}/chat/{session_id}",
                json={"message": message},
                timeout=(self.timeout, 120),  # (connect_timeout, read_timeout)
                stream=True,
            )
            if resp.status_code == 200:
                # 读取 SSE 流直到 done 事件，确保消息完全处理并持久化
                received_done = False
                for line in resp.iter_lines(decode_unicode=True):
                    if line and line.startswith("data: "):
                        try:
                            event = json.loads(line[6:])
                            if event.get("type") == "done":
                                received_done = True
                                break
                        except json.JSONDecodeError:
                            continue
                resp.close()
                return received_done
            elif resp.status_code == 404:
                resp.close()
                return False  # session not found
            else:
                print(f"[API] 发送消息失败 (HTTP {resp.status_code}): {resp.text[:200]}")
                resp.close()
                return False
        except requests.exceptions.Timeout:
            print(f"[API] 发送消息超时（agent 处理过久）")
            return False
        except Exception as e:
            print(f"[API] 发送消息异常: {e}")
            return False

    def session_exists(self, session_id: str) -> bool:
        """检查会话是否存在"""
        try:
            resp = requests.get(
                f"{self.base_url}/sessions/{session_id}",
                timeout=self.timeout,
            )
            return resp.status_code == 200
        except Exception:
            return False


# ============================================================
# 定时器引擎
# ============================================================

class TimerEngine:
    """后台线程驱动的定时器引擎"""

    def __init__(self, config: dict, api: WebUIClient, on_trigger=None, on_status_change=None):
        self.config = config
        self.api = api
        self.on_trigger = on_trigger  # callback(timer_id, message)
        self.on_status_change = on_status_change  # callback()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _loop(self):
        """主循环：每秒检查一次"""
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                traceback.print_exc()
            self._stop_event.wait(1.0)

    def _tick(self):
        now = time.time()
        timers = self.config.get("timers", {})
        changed = False

        for tid, tmr in list(timers.items()):
            if not tmr.get("enabled", False):
                continue

            should_fire = False
            mode = tmr.get("mode", "interval")
            last = tmr.get("last_triggered")

            if mode == "interval":
                interval_seconds = tmr.get("interval_seconds", 3600)
                if last is None or (now - last >= interval_seconds):
                    should_fire = True

            elif mode == "scheduled":
                scheduled_time = tmr.get("scheduled_time", "")  # "HH:MM"
                weekdays_only = tmr.get("weekdays_only", False)
                if scheduled_time:
                    now_dt = datetime.now()
                    target_str = now_dt.strftime("%Y-%m-%d") + " " + scheduled_time
                    try:
                        target_dt = datetime.strptime(target_str, "%Y-%m-%d %H:%M")
                    except ValueError:
                        continue

                    # 检查工作日
                    if weekdays_only and now_dt.weekday() >= 5:
                        continue

                    # 今天目标时间已过？跳过
                    if now_dt < target_dt:
                        continue

                    # 检查今天是否已触发
                    today_str = now_dt.strftime("%Y-%m-%d")
                    last_triggered_date = ""
                    if last:
                        last_dt = datetime.fromtimestamp(last)
                        last_triggered_date = last_dt.strftime("%Y-%m-%d")

                    if last_triggered_date != today_str and now_dt >= target_dt:
                        should_fire = True

            if should_fire:
                success = self._fire_timer(tid, tmr)
                if success:
                    tmr["last_triggered"] = now
                    changed = True
                    if self.on_trigger:
                        self.on_trigger(tid, tmr.get("task", ""))

        if changed:
            save_config(self.config)

        # 每次 tick 都刷新列表，让倒计时每秒更新
        if self.on_status_change:
            self.on_status_change()

    def _fire_timer(self, tid: str, tmr: dict) -> bool:
        """执行一次触发"""
        session_id = tmr.get("session_id")
        task = tmr.get("task", "定时任务触发")
        timer_name = tmr.get("name", tid)

        message = f"[定时任务] {timer_name}\n{task}"

        # 如果没有 session_id 或 session 已丢失，重建
        if not session_id or not self.api.session_exists(session_id):
            print(f"[{datetime.now():%H:%M:%S}] 会话丢失，重建: {timer_name}")
            new_sid = self.api.create_session(timer_name)
            if new_sid:
                tmr["session_id"] = new_sid
                session_id = new_sid
                save_config(self.config)
            else:
                print(f"[{datetime.now():%H:%M:%S}] ❌ 重建会话失败: {timer_name}")
                return False

        success = self.api.send_message(session_id, message)
        if success:
            print(f"[{datetime.now():%H:%M:%S}] ✅ 触发成功: {timer_name}")
        else:
            print(f"[{datetime.now():%H:%M:%S}] ❌ 发送失败: {timer_name}")
            # 发送失败时检查是否是 session 丢失（send_message 返回 False 可能是 404）
            if not self.api.session_exists(session_id):
                print(f"[{datetime.now():%H:%M:%S}] 会话丢失（404），重建...")
                new_sid = self.api.create_session(timer_name)
                if new_sid:
                    tmr["session_id"] = new_sid
                    session_id = new_sid
                    save_config(self.config)
                    # 重试
                    success = self.api.send_message(session_id, message)
                    if success:
                        print(f"[{datetime.now():%H:%M:%S}] ✅ 重试成功: {timer_name}")

        return success

    def trigger_now(self, tid: str) -> bool:
        """手动立即触发"""
        timers = self.config.get("timers", {})
        tmr = timers.get(tid)
        if not tmr:
            return False
        success = self._fire_timer(tid, tmr)
        if success:
            tmr["last_triggered"] = time.time()
            save_config(self.config)
        return success


# ============================================================
# 定时器编辑对话框
# ============================================================

class TimerDialog(tk.Toplevel):
    """新建 / 编辑定时器的对话框"""

    def __init__(self, parent, config: dict, timer_id: Optional[str] = None):
        super().__init__(parent)
        self.config = config
        self.timer_id = timer_id
        self.result: Optional[dict] = None  # 返回定时器数据

        self.title("编辑定时器" if timer_id else "新建定时器")
        self.geometry("480x420")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self._mode_var = tk.StringVar(value="interval")
        self._weekdays_var = tk.BooleanVar(value=False)

        self._build_ui()

        # 如果是编辑模式，填充现有数据
        if timer_id:
            self._load_timer_data()

        # 居中
        self.update_idletasks()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        px, py = parent.winfo_x(), parent.winfo_y()
        w, h = self.winfo_width(), self.winfo_height()
        x = px + (pw - w) // 2
        y = py + (ph - h) // 2
        self.geometry(f"+{x}+{y}")

    def _build_ui(self):
        pad = {"padx": 12, "pady": 4}

        # --- 名称 ---
        ttk.Label(self, text="定时器名称:").pack(anchor="w", **pad)
        self._name_entry = ttk.Entry(self, width=50)
        self._name_entry.pack(fill="x", padx=12, pady=(0, 8))

        # --- 模式选择 ---
        ttk.Label(self, text="触发模式:").pack(anchor="w", **pad)
        mode_frame = ttk.Frame(self)
        mode_frame.pack(fill="x", padx=12, pady=(0, 4))
        ttk.Radiobutton(mode_frame, text="间隔模式（每 N 分钟触发）",
                        variable=self._mode_var, value="interval",
                        command=self._on_mode_change).pack(anchor="w")
        ttk.Radiobutton(mode_frame, text="定时模式（每天指定时间触发）",
                        variable=self._mode_var, value="scheduled",
                        command=self._on_mode_change).pack(anchor="w")

        # --- 间隔模式参数 ---
        self._interval_frame = ttk.LabelFrame(self, text="间隔设置")
        self._interval_frame.pack(fill="x", padx=12, pady=(4, 8))

        ttk.Label(self._interval_frame, text="间隔:").pack(side="left", padx=(8, 4), pady=8)
        self._interval_value = ttk.Spinbox(self._interval_frame, from_=1, to=1440, width=6)
        self._interval_value.set("30")
        self._interval_value.pack(side="left", pady=8)
        ttk.Label(self._interval_frame, text="分钟").pack(side="left", pady=8)

        ttk.Label(self._interval_frame, text="（范围: 1-1440 分钟）").pack(
            side="left", padx=12, pady=8)

        # --- 定时模式参数 ---
        self._scheduled_frame = ttk.LabelFrame(self, text="定时设置")
        self._scheduled_frame.pack(fill="x", padx=12, pady=(0, 8))

        row1 = ttk.Frame(self._scheduled_frame)
        row1.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(row1, text="每天触发时间:").pack(side="left")
        self._hour_spin = ttk.Spinbox(row1, from_=0, to=23, width=3, format="%02.0f")
        self._hour_spin.set("08")
        self._hour_spin.pack(side="left", padx=(8, 0))
        ttk.Label(row1, text=":").pack(side="left")
        self._min_spin = ttk.Spinbox(row1, from_=0, to=59, width=3, format="%02.0f")
        self._min_spin.set("00")
        self._min_spin.pack(side="left")

        self._wd_check = ttk.Checkbutton(
            self._scheduled_frame, text="仅工作日（周一至周五）",
            variable=self._weekdays_var)
        self._wd_check.pack(anchor="w", padx=8, pady=(0, 8))

        # --- 任务内容 ---
        ttk.Label(self, text="任务描述（触发时发送给 Xenon 的消息）:").pack(
            anchor="w", **pad)
        self._task_text = tk.Text(self, height=4, width=50)
        self._task_text.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        # --- 按钮 ---
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(btn_frame, text="取消", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(btn_frame, text="保存", command=self._on_save).pack(side="right")

        self._on_mode_change()

    def _on_mode_change(self):
        mode = self._mode_var.get()
        if mode == "interval":
            self._scheduled_frame.pack_forget()
            self._interval_frame.pack(fill="x", padx=12, pady=(4, 8))
        else:
            self._interval_frame.pack_forget()
            self._scheduled_frame.pack(fill="x", padx=12, pady=(0, 8))

    def _load_timer_data(self):
        timers = self.config.get("timers", {})
        tmr = timers.get(self.timer_id)
        if not tmr:
            return
        self._name_entry.insert(0, tmr.get("name", ""))
        self._mode_var.set(tmr.get("mode", "interval"))
        self._on_mode_change()
        if tmr.get("mode") == "interval":
            self._interval_value.set(str(int(tmr.get("interval_seconds", 1800) / 60)))
        else:
            st = tmr.get("scheduled_time", "08:00")
            parts = st.split(":")
            self._hour_spin.set(parts[0])
            self._min_spin.set(parts[1] if len(parts) > 1 else "00")
            self._weekdays_var.set(tmr.get("weekdays_only", False))
        self._task_text.insert("1.0", tmr.get("task", ""))

    def _on_save(self):
        name = self._name_entry.get().strip()
        if not name:
            messagebox.showwarning("提示", "请输入定时器名称", parent=self)
            return

        task = self._task_text.get("1.0", "end").strip()
        if not task:
            messagebox.showwarning("提示", "请输入任务描述", parent=self)
            return

        mode = self._mode_var.get()

        data = {
            "name": name,
            "mode": mode,
            "task": task,
        }

        if mode == "interval":
            try:
                minutes = int(self._interval_value.get())
                if minutes < 1:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("提示", "间隔分钟数必须是 1-1440 的整数", parent=self)
                return
            data["interval_seconds"] = minutes * 60
        else:
            try:
                h = int(self._hour_spin.get())
                m = int(self._min_spin.get())
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    raise ValueError
            except ValueError:
                messagebox.showwarning("提示", "时间格式无效", parent=self)
                return
            data["scheduled_time"] = f"{h:02d}:{m:02d}"
            data["weekdays_only"] = self._weekdays_var.get()

        # 编辑模式：保留原有字段
        if self.timer_id:
            old = self.config.get("timers", {}).get(self.timer_id, {})
            data["session_id"] = old.get("session_id")
            data["enabled"] = old.get("enabled", False)
            data["last_triggered"] = None  # 编辑后重置触发记录
        else:
            data["session_id"] = None
            data["enabled"] = False
            data["last_triggered"] = None

        # 验证通过前先退出（session 在保存时由主窗口创建）
        self.result = data
        self.destroy()


# ============================================================
# 主窗口
# ============================================================

class TimerApp:
    """定时器管理主窗口"""

    def __init__(self):
        self.config = load_config()
        self.api = WebUIClient(self.config.get("webui_url", WEBUI_BASE_URL))
        self.engine: Optional[TimerEngine] = None

        self.root = tk.Tk()
        self.root.title("⏰ Xenon 定时器管理")
        self.root.geometry("680x520")
        self.root.minsize(550, 400)

        self._log_lines: list[str] = []  # [(timestamp, text)]
        self._building = False

        self._build_ui()
        self._refresh_list()

        # 启动引擎
        self.engine = TimerEngine(
            self.config, self.api,
            on_trigger=self._on_trigger,
            on_status_change=self._refresh_list,
        )
        self.engine.start()

        # 窗口关闭
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # --- 工具栏 ---
        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill="x", padx=8, pady=(8, 4))

        ttk.Button(toolbar, text="＋ 新建定时器", command=self._add_timer).pack(side="left", padx=(0, 4))
        ttk.Button(toolbar, text="✎ 编辑", command=self._edit_timer).pack(side="left", padx=4)
        ttk.Button(toolbar, text="✕ 删除", command=self._delete_timer).pack(side="left", padx=4)
        ttk.Button(toolbar, text="▶ 启用", command=self._enable_timer).pack(side="left", padx=4)
        ttk.Button(toolbar, text="⏸ 禁用", command=self._disable_timer).pack(side="left", padx=4)
        ttk.Button(toolbar, text="⚡ 立即触发", command=self._trigger_now).pack(side="left", padx=4)

        # --- 定时器列表 ---
        list_frame = ttk.LabelFrame(self.root, text="定时器列表")
        list_frame.pack(fill="both", expand=True, padx=8, pady=4)

        columns = ("name", "mode_display", "schedule_display", "status")
        self._tree = ttk.Treeview(
            list_frame, columns=columns, show="headings",
            selectmode="browse", height=8,
        )
        self._tree.heading("name", text="名称")
        self._tree.heading("mode_display", text="模式")
        self._tree.heading("schedule_display", text="时间设置")
        self._tree.heading("status", text="状态")

        self._tree.column("name", width=160, minwidth=100)
        self._tree.column("mode_display", width=80, minwidth=60)
        self._tree.column("schedule_display", width=200, minwidth=120)
        self._tree.column("status", width=80, minwidth=60)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)

        self._tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scrollbar.pack(side="right", fill="y", padx=(0, 8), pady=8)

        # 双击编辑
        self._tree.bind("<Double-1>", lambda e: self._edit_timer())

        # --- 日志区 ---
        log_frame = ttk.LabelFrame(self.root, text="运行日志")
        log_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self._log_text = tk.Text(log_frame, height=6, state="disabled", wrap="word")
        self._log_text.pack(fill="both", expand=True, padx=8, pady=8)

        # 日志区颜色标签
        self._log_text.tag_configure("success", foreground="#2e7d32")
        self._log_text.tag_configure("error", foreground="#c62828")
        self._log_text.tag_configure("info", foreground="#1565c0")
        self._log_text.tag_configure("time", foreground="#757575")

    # ============================================================
    # 列表刷新
    # ============================================================

    def _refresh_list(self):
        """刷新定时器列表（可从其他线程调用）"""
        if self._building:
            return
        self._building = True
        try:
            self.root.after(0, self._do_refresh_list)
        finally:
            self._building = False

    def _do_refresh_list(self):
        # 保存选中
        selection = self._tree.selection()
        selected_id = None
        if selection:
            values = self._tree.item(selection[0], "values")
            if values:
                selected_id = self._id_from_name(values[0])

        # 清空重建
        for item in self._tree.get_children():
            self._tree.delete(item)

        timers = self.config.get("timers", {})
        for tid in sorted(timers.keys()):
            tmr = timers[tid]
            name = tmr.get("name", tid)
            mode = tmr.get("mode", "interval")
            enabled = tmr.get("enabled", False)

            mode_display = "间隔" if mode == "interval" else "定时"
            if mode == "interval":
                minutes = int(tmr.get("interval_seconds", 1800) / 60)
                base = ""
                if minutes >= 60:
                    base = f"每 {minutes // 60} 小时" + (
                        f" {minutes % 60} 分" if minutes % 60 else "")
                else:
                    base = f"每 {minutes} 分钟"
                # 倒计时
                if enabled:
                    last = tmr.get("last_triggered")
                    interval_seconds = tmr.get("interval_seconds", 3600)
                    if last:
                        elapsed = time.time() - last
                        remaining = max(0, interval_seconds - elapsed)
                    else:
                        remaining = 0  # 首次，tick 会立即触发
                    remaining_str = self._format_countdown(remaining)
                    schedule_display = f"{base}  ({remaining_str})"
                else:
                    schedule_display = base
            else:
                st = tmr.get("scheduled_time", "08:00")
                wd = " (工作日)" if tmr.get("weekdays_only") else ""
                # 倒计时
                if enabled:
                    now_dt = datetime.now()
                    target_str = now_dt.strftime("%Y-%m-%d") + " " + st
                    try:
                        target_dt = datetime.strptime(target_str, "%Y-%m-%d %H:%M")
                    except ValueError:
                        target_dt = now_dt
                    if now_dt >= target_dt:
                        # 今天已过 → 明天
                        target_dt += timedelta(days=1)
                        remaining = (target_dt - now_dt).total_seconds()
                        tomorrow_str = target_dt.strftime("%m-%d %H:%M")
                        remaining_str = self._format_countdown(remaining)
                        schedule_display = f"每天 {st}{wd}  (明天 {tomorrow_str}, {remaining_str})"
                    else:
                        remaining = (target_dt - now_dt).total_seconds()
                        remaining_str = self._format_countdown(remaining)
                        schedule_display = f"每天 {st}{wd}  ({remaining_str})"
                else:
                    schedule_display = f"每天 {st}{wd}"

            status = "● 运行中" if enabled else "○ 已停止"
            tag = "enabled" if enabled else "disabled"

            self._tree.insert(
                "", "end", iid=tid,
                values=(name, mode_display, schedule_display, status),
                tags=(tag,),
            )

        # 恢复选中
        if selected_id and selected_id in timers:
            self._tree.selection_set(selected_id)
            self._tree.see(selected_id)

        # 颜色
        self._tree.tag_configure("enabled", foreground="#2e7d32")
        self._tree.tag_configure("disabled", foreground="#9e9e9e")

    def _id_from_name(self, name: str) -> Optional[str]:
        timers = self.config.get("timers", {})
        for tid, tmr in timers.items():
            if tmr.get("name") == name:
                return tid
        return None

    @staticmethod
    def _format_countdown(seconds: float) -> str:
        """将秒数格式化为人类可读的倒计时"""
        seconds = max(0, int(seconds))
        if seconds < 60:
            return f"还有 {seconds} 秒"
        elif seconds < 3600:
            m = seconds // 60
            s = seconds % 60
            return f"还有 {m} 分 {s} 秒"
        elif seconds < 86400:
            h = seconds // 3600
            m = (seconds % 3600) // 60
            return f"还有 {h} 小时 {m} 分"
        else:
            d = seconds // 86400
            h = (seconds % 86400) // 3600
            return f"还有 {d} 天 {h} 小时"

    def _get_selected_id(self) -> Optional[str]:
        selection = self._tree.selection()
        if not selection:
            return None
        return selection[0]

    # ============================================================
    # 操作
    # ============================================================

    def _add_timer(self):
        dialog = TimerDialog(self.root, self.config)
        self.root.wait_window(dialog)
        if not dialog.result:
            return

        data = dialog.result
        name = data["name"]

        # 创建 WebUI 会话
        self._log(f"正在为「{name}」创建会话...", "info")
        session_id = self.api.create_session(name)
        if session_id:
            data["session_id"] = session_id
            self._log(f"会话已创建: {session_id[:8]}...", "success")
        else:
            self._log(f"⚠️ 会话创建失败，触发时会自动重建", "error")

        # 保存
        timer_id = uuid.uuid4().hex[:12]
        self.config.setdefault("timers", {})[timer_id] = data
        save_config(self.config)
        self._refresh_list()
        self._log(f"定时器「{name}」已创建（未启用）", "info")

    def _edit_timer(self):
        tid = self._get_selected_id()
        if not tid:
            messagebox.showinfo("提示", "请先选择一个定时器")
            return

        tmr = self.config["timers"].get(tid)
        if not tmr:
            return

        dialog = TimerDialog(self.root, self.config, timer_id=tid)
        self.root.wait_window(dialog)
        if not dialog.result:
            return

        data = dialog.result
        name = data["name"]

        # 如果名称变了，需要重建会话
        old_name = tmr.get("name", "")
        if name != old_name and data.get("session_id"):
            self._log(f"名称已变更，重建会话...", "info")
            new_sid = self.api.create_session(name)
            if new_sid:
                data["session_id"] = new_sid
                self._log(f"会话已重建: {new_sid[:8]}...", "success")

        self.config["timers"][tid] = data
        save_config(self.config)
        self._refresh_list()
        self._log(f"定时器「{name}」已更新", "info")

    def _delete_timer(self):
        tid = self._get_selected_id()
        if not tid:
            messagebox.showinfo("提示", "请先选择一个定时器")
            return

        tmr = self.config["timers"].get(tid, {})
        name = tmr.get("name", tid)

        if not messagebox.askyesno("确认删除", f"确定要删除定时器「{name}」吗？"):
            return

        del self.config["timers"][tid]
        save_config(self.config)
        self._refresh_list()
        self._log(f"定时器「{name}」已删除", "info")

    def _enable_timer(self):
        tid = self._get_selected_id()
        if not tid:
            messagebox.showinfo("提示", "请先选择一个定时器")
            return

        tmr = self.config["timers"].get(tid)
        if not tmr:
            return

        name = tmr.get("name", tid)

        # 确保有 session
        if not tmr.get("session_id"):
            self._log(f"正在为「{name}」创建会话...", "info")
            session_id = self.api.create_session(name)
            if session_id:
                tmr["session_id"] = session_id
                self._log(f"会话已创建: {session_id[:8]}...", "success")
            else:
                self._log(f"⚠️ 会话创建失败", "error")
                return

        tmr["enabled"] = True
        tmr["last_triggered"] = None  # 启用时重置，立即生效
        save_config(self.config)
        self._refresh_list()
        self._log(f"✅ 定时器「{name}」已启用", "success")

    def _disable_timer(self):
        tid = self._get_selected_id()
        if not tid:
            messagebox.showinfo("提示", "请先选择一个定时器")
            return

        tmr = self.config["timers"].get(tid)
        if not tmr:
            return

        name = tmr.get("name", tid)
        tmr["enabled"] = False
        save_config(self.config)
        self._refresh_list()
        self._log(f"⏸ 定时器「{name}」已禁用", "info")

    def _trigger_now(self):
        tid = self._get_selected_id()
        if not tid:
            messagebox.showinfo("提示", "请先选择一个定时器")
            return

        tmr = self.config["timers"].get(tid)
        if not tmr:
            return

        name = tmr.get("name", tid)
        self._log(f"⚡ 手动触发: {name} ...", "info")

        if self.engine:
            success = self.engine.trigger_now(tid)
        else:
            success = False

        if success:
            self._log(f"✅ 触发成功: {name}", "success")
        else:
            self._log(f"❌ 触发失败: {name}", "error")

        self._refresh_list()

    # ============================================================
    # 日志
    # ============================================================

    def _on_trigger(self, timer_id: str, task: str):
        """引擎触发回调"""
        timers = self.config.get("timers", {})
        name = timers.get(timer_id, {}).get("name", timer_id)
        self.root.after(0, lambda: self._log(f"⏰ 触发: {name} → {task[:50]}...", "info"))

    def _log(self, text: str, level: str = "info"):
        """追加日志（线程安全）"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {text}\n"
        self.root.after(0, lambda: self._do_log(line, level))

    def _do_log(self, line: str, level: str):
        self._log_text.configure(state="normal")
        self._log_text.insert("end", line, (level, "time"))
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

        # 限制日志行数
        lines = int(self._log_text.index("end-1c").split(".")[0])
        if lines > 500:
            self._log_text.configure(state="normal")
            self._log_text.delete("1.0", f"{lines - 400}.0")
            self._log_text.configure(state="disabled")

    # ============================================================
    # 生命周期
    # ============================================================

    def _on_close(self):
        if self.engine:
            self.engine.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ============================================================
# 入口
# ============================================================

def main():
    # 检查依赖
    try:
        import requests
    except ImportError:
        print("请先安装 requests: pip install requests")
        sys.exit(1)

    app = TimerApp()
    app.run()


if __name__ == "__main__":
    main()
