# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from tkinter import messagebox
import tkinter as tk


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "launcher_settings.json"
REQUIREMENTS_PATH = PROJECT_ROOT / "requirements.txt"
VENV_DIR = PROJECT_ROOT / "venv"
IS_WINDOWS = os.name == "nt"
VENV_PYTHON = VENV_DIR / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")

CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CHAT_API_KEY_ENV_NAMES = ("DEEPSEEK_API_KEY",)


def load_settings() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(settings: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as handle:
        json.dump(settings, handle, ensure_ascii=False, indent=2)


def first_env_value(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return ""


def venv_python_path() -> Path:
    return VENV_PYTHON


def runnable_python() -> Path:
    return venv_python_path() if venv_python_path().exists() else Path(sys.executable)


def base_python_for_venv() -> Path:
    executable = Path(sys.executable)
    if IS_WINDOWS and executable.name.lower() == "pythonw.exe":
        python_exe = executable.with_name("python.exe")
        if python_exe.exists():
            return python_exe
    return executable


def is_process_running(process: subprocess.Popen | None) -> bool:
    return process is not None and process.poll() is None


class XenonLauncher(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Xenon 启动器")
        self.geometry("530x390")
        self.minsize(480, 340)

        self.settings = load_settings()
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.ui_queue: queue.Queue = queue.Queue()
        self.install_thread: threading.Thread | None = None
        self.terminal_process: subprocess.Popen | None = None
        self.webui_process: subprocess.Popen | None = None
        credentials = self.settings.get("credentials") if isinstance(self.settings.get("credentials"), dict) else {}
        saved_chat_key = credentials.get("chat_api_key") or self.settings.get("api_key", "")
        self.chat_api_key_var = tk.StringVar(value=saved_chat_key or first_env_value(CHAT_API_KEY_ENV_NAMES))
        self.secret_entries: list[tk.Entry] = []
        self.show_key_var = tk.BooleanVar(value=False)
        self.host_var = tk.StringVar(value=self.settings.get("webui_host", "127.0.0.1"))
        self.port_var = tk.StringVar(value=str(self.settings.get("webui_port", "8000")))
        self.open_browser_var = tk.BooleanVar(value=self.settings.get("open_browser", True))
        self.install_status_var = tk.StringVar(value="依赖状态：待检查")
        self.terminal_status_var = tk.StringVar(value="终端：未启动")
        self.webui_status_var = tk.StringVar(value="WebUI：未启动")

        self._configure_style()
        self._build_ui()
        self.after(100, self._drain_log_queue)
        self.after(100, self._drain_ui_queue)
        self.after(1200, self._poll_processes)
        self._log("启动器已就绪。")
        if venv_python_path().exists():
            self.install_status_var.set("依赖状态：已发现项目虚拟环境")
        else:
            self.install_status_var.set("依赖状态：未发现 venv，可点击安装按钮")

    def _configure_style(self) -> None:
        self.configure(bg="#e0d1b0")

    def _build_ui(self) -> None:
        # ── 顶栏 ──
        top_bar = tk.Frame(self, bg="#9d803f", height=44)
        top_bar.pack(fill="x")
        top_bar.pack_propagate(False)
        tk.Label(
            top_bar, text="XENON", bg="#9d803f", fg="#ffffff",
            font=("Microsoft YaHei", 22, "bold"),
        ).pack(expand=True)

        # ── 内容区 ──
        content = tk.Frame(self, bg="#e0d1b0", padx=20, pady=10)
        content.pack(fill="both", expand=True)

        # ── 行1: api-key ──
        row1 = tk.Frame(content, bg="#e0d1b0")
        row1.pack(fill="x", pady=(0, 6))
        tk.Label(row1, text="api-key", bg="#e0d1b0", fg="#594e14",
                 font=("Microsoft YaHei", 10)).pack(side="left", padx=(0, 8))
        api_frame = tk.Frame(row1, bg="#9f9770")
        api_frame.pack(side="left", fill="x", expand=True, padx=(0, 8))
        api_entry = tk.Entry(
            api_frame, textvariable=self.chat_api_key_var,
            bg="#9f9770", fg="#ffffff", relief="flat",
            font=("Consolas", 10), show="*",
            insertbackground="#ffffff",
        )
        api_entry.pack(fill="both", expand=True)
        self.secret_entries.append(api_entry)
        tk.Button(
            row1, text="保存", bg="#5d562c", fg="#f5f4ed",
            relief="flat", font=("Microsoft YaHei", 9, "bold"),
            padx=14, pady=2, bd=0,
            command=self._save_settings_from_ui,
        ).pack(side="left")

        # ── 行2: 依赖 + web-ui（同行排列） ──
        row2 = tk.Frame(content, bg="#e0d1b0")
        row2.pack(fill="x", pady=(0, 10))
        tk.Label(row2, text="依赖", bg="#e0d1b0", fg="#594e14",
                 font=("Microsoft YaHei", 10)).pack(side="left", padx=(0, 4))
        self.install_button = tk.Button(
            row2, text="安装", bg="#5d562c", fg="#f5f4ed",
            relief="flat", font=("Microsoft YaHei", 9, "bold"),
            padx=8, pady=2, bd=0,
            command=self.install_dependencies,
        )
        self.install_button.pack(side="left", padx=(0, 56))
        tk.Label(row2, text="web-ui：", bg="#e0d1b0", fg="#594e14",
                 font=("Microsoft YaHei",10)).pack(side="left", padx=(0, 4))
        tk.Label(row2, text="Host", bg="#e0d1b0", fg="#594e14",
                 font=("Microsoft YaHei", 9)).pack(side="left", padx=(0, 3))
        tk.Entry(
            row2, textvariable=self.host_var,
            bg="#9f9770", fg="#ffffff", relief="flat",
            font=("Microsoft YaHei", 9), width=11,
            insertbackground="#ffffff",
        ).pack(side="left", padx=(0, 8))
        tk.Label(row2, text="Port", bg="#e0d1b0", fg="#594e14",
                 font=("Microsoft YaHei", 9)).pack(side="left", padx=(0, 3))
        tk.Entry(
            row2, textvariable=self.port_var,
            bg="#9f9770", fg="#ffffff", relief="flat",
            font=("Microsoft YaHei", 9), width=5,
            insertbackground="#ffffff",
        ).pack(side="left")

        # ── 行3: 四个操作按钮（整体居中）──
        row3 = tk.Frame(content, bg="#e0d1b0")
        row3.pack(fill="x", pady=(0, 8))
        btn_frame = tk.Frame(row3, bg="#e0d1b0")
        btn_frame.pack(expand=True)
        btn_cfg = {"bg": "#5d562c", "fg": "#f5f4ed", "relief": "flat",
                   "font": ("Microsoft YaHei", 9, "bold"), "width": 10, "pady": 6, "bd": 0}
        tk.Button(btn_frame, text="启动终端", command=self.start_terminal, **btn_cfg).pack(
            side="left", padx=(0, 30))
        tk.Button(btn_frame, text="启动Webui", command=self.start_webui, **btn_cfg).pack(
            side="left", padx=(0, 30))
        tk.Button(btn_frame, text="停止Webui", command=self.stop_webui, **btn_cfg).pack(
            side="left", padx=(0, 30))
        tk.Button(btn_frame, text="打开浏览器", command=self.open_webui, **btn_cfg).pack(
            side="left")

        # ── 行4: 状态 ──
        row4 = tk.Frame(content, bg="#e0d1b0")
        row4.pack(fill="x", pady=(0, 4))
        tk.Label(row4, textvariable=self.install_status_var, bg="#e0d1b0",
                 fg="#5d562c", font=("Microsoft YaHei", 8)).pack(side="left", padx=(0, 12))
        tk.Label(row4, textvariable=self.terminal_status_var, bg="#e0d1b0",
                 fg="#5d562c", font=("Microsoft YaHei", 8)).pack(side="left", padx=(0, 12))
        tk.Label(row4, textvariable=self.webui_status_var, bg="#e0d1b0",
                 fg="#5d562c", font=("Microsoft YaHei", 8)).pack(side="left")

        # ── 行5: 选项 ──
        opt_frame = tk.Frame(content, bg="#e0d1b0")
        opt_frame.pack(fill="x", pady=(0, 6))
        tk.Checkbutton(
            opt_frame, text="显示密钥", variable=self.show_key_var,
            bg="#e0d1b0", fg="#594e14", selectcolor="#e0d1b0",
            activebackground="#e0d1b0", activeforeground="#594e14",
            font=("Microsoft YaHei", 8), relief="flat",
            command=self._toggle_api_visibility,
        ).pack(side="left", padx=(0, 12))
        tk.Checkbutton(
            opt_frame, text="启动WebUI后自动打开浏览器",
            variable=self.open_browser_var,
            bg="#e0d1b0", fg="#594e14", selectcolor="#e0d1b0",
            activebackground="#e0d1b0", activeforeground="#594e14",
            font=("Microsoft YaHei", 8), relief="flat",
        ).pack(side="left")

        # ── 运行日志 ──
        tk.Label(content, text="运行日志", bg="#e0d1b0", fg="#272410",
                 font=("Microsoft YaHei", 9, "bold")).pack(anchor="w", pady=(0, 4))
        log_frame = tk.Frame(content, bg="#5d562c")
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            log_frame, height=5, bg="#5d562c", fg="#f5f4ed",
            relief="flat", wrap="word",
            font=("Microsoft YaHei", 9), insertbackground="#f5f4ed",
            padx=6, pady=4,
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    def _toggle_api_visibility(self) -> None:
        show = "" if self.show_key_var.get() else "*"
        for entry in self.secret_entries:
            entry.configure(show=show)

    def _collect_settings(self) -> dict:
        credentials = {}
        chat_api_key = self.chat_api_key_var.get().strip()
        if chat_api_key:
            credentials["chat_api_key"] = chat_api_key
        settings = {
            "webui_host": self.host_var.get().strip() or "127.0.0.1",
            "webui_port": self.port_var.get().strip() or "8000",
            "open_browser": bool(self.open_browser_var.get()),
            "credentials": credentials,
        }
        return settings

    def _save_settings_from_ui(self) -> None:
        try:
            settings = self._collect_settings()
            save_settings(settings)
            self.settings = settings
            self._log(f"配置已保存到 {CONFIG_PATH}")
            messagebox.showinfo("已保存", "配置已保存。")
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))

    def _build_child_env(self) -> dict[str, str]:
        settings = self._collect_settings()
        env = os.environ.copy()
        credentials = settings.get("credentials", {})
        chat_key = credentials.get("chat_api_key") or env.get("DEEPSEEK_API_KEY", "")
        if chat_key:
            env["DEEPSEEK_API_KEY"] = chat_key
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["XENON_WEBUI_HOST"] = str(settings.get("webui_host", "127.0.0.1"))
        env["XENON_WEBUI_PORT"] = str(settings.get("webui_port", "8000"))
        env.setdefault("XENON_WEBUI_PREWARM", "auto")
        env.setdefault("XENON_WEBUI_FILE_WATCHER", "auto")
        env.setdefault("XENON_WEBUI_BACKGROUND_THEME", "1")
        return env

    def _validate_launch_ready(self) -> bool:
        if not venv_python_path().exists():
            messagebox.showwarning("缺少虚拟环境", "未找到 venv\\Scripts\\python.exe，请先点击安装按钮。")
            return False
        if not (self.chat_api_key_var.get().strip() or os.getenv("DEEPSEEK_API_KEY")):
            messagebox.showwarning("缺少 API Key", "请先填写并保存 DeepSeek API Key。")
            return False
        return True

    def install_dependencies(self) -> None:
        if self.install_thread and self.install_thread.is_alive():
            self._log("依赖安装正在运行。")
            return
        self.install_button.configure(state="disabled")
        self.install_status_var.set("依赖状态：安装中")
        self.install_thread = threading.Thread(target=self._install_dependencies_worker, daemon=True)
        self.install_thread.start()

    def _install_dependencies_worker(self) -> None:
        try:
            if not REQUIREMENTS_PATH.exists():
                raise FileNotFoundError(f"未找到 {REQUIREMENTS_PATH}")

            if not venv_python_path().exists():
                self._log("未发现 venv，开始创建虚拟环境。")
                self._run_command([str(base_python_for_venv()), "-m", "venv", str(VENV_DIR)], "创建虚拟环境")
            else:
                self._log(f"使用现有虚拟环境：{venv_python_path()}")

            python = str(venv_python_path())
            self._run_command([python, "-m", "pip", "install", "--upgrade", "pip"], "升级 pip")
            self._run_command([python, "-m", "pip", "install", "-r", str(REQUIREMENTS_PATH)], "安装 requirements.txt")
            self._log("依赖安装完成。")
            self._post_ui(lambda: self.install_status_var.set("依赖状态：安装完成"))
        except Exception as exc:
            self._log(f"依赖安装失败：{exc}")
            self._post_ui(lambda: self.install_status_var.set("依赖状态：安装失败"))
        finally:
            self._post_ui(lambda: self.install_button.configure(state="normal"))

    def _run_command(self, args: list[str], title: str) -> None:
        self._log(f"开始：{title}")
        self._log("$ " + " ".join(f'"{arg}"' if " " in arg else arg for arg in args))
        process = subprocess.Popen(
            args,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        assert process.stdout is not None
        for line in process.stdout:
            self.log_queue.put(line)
        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError(f"{title} 失败，退出码 {return_code}")
        self._log(f"完成：{title}")

    def start_terminal(self) -> None:
        if is_process_running(self.terminal_process):
            self._log("终端程序已经在运行。")
            return
        if not self._validate_launch_ready():
            return
        self._save_settings_silently()
        command = [str(venv_python_path()), str(PROJECT_ROOT / "Xenon.py")]
        try:
            self.terminal_process = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                env=self._build_child_env(),
                creationflags=CREATE_NEW_CONSOLE if IS_WINDOWS else 0,
            )
            self.terminal_status_var.set(f"终端：运行中 PID {self.terminal_process.pid}")
            self._log(f"已启动终端程序，PID {self.terminal_process.pid}")
        except OSError as exc:
            messagebox.showerror("启动失败", str(exc))
            self._log(f"终端程序启动失败：{exc}")

    def start_webui(self) -> None:
        if is_process_running(self.webui_process):
            self._log("WebUI 已经在运行。")
            if self.open_browser_var.get():
                self.open_webui()
            return
        if not self._validate_launch_ready():
            return
        self._save_settings_silently()
        command = [str(venv_python_path()), str(PROJECT_ROOT / "webui" / "main.py")]
        try:
            self.webui_process = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                env=self._build_child_env(),
                creationflags=CREATE_NEW_CONSOLE if IS_WINDOWS else 0,
            )
            self.webui_status_var.set(f"WebUI：运行中 PID {self.webui_process.pid}")
            self._log(f"已启动 WebUI，PID {self.webui_process.pid}")
            if self.open_browser_var.get():
                self.after(2500, self.open_webui)
        except OSError as exc:
            messagebox.showerror("启动失败", str(exc))
            self._log(f"WebUI 启动失败：{exc}")

    def stop_webui(self) -> None:
        if not is_process_running(self.webui_process):
            self.webui_status_var.set("WebUI：未启动")
            self._log("没有正在运行的 WebUI 进程。")
            return
        assert self.webui_process is not None
        pid = self.webui_process.pid
        self.webui_process.terminate()
        self._log(f"已请求停止 WebUI，PID {pid}")
        self.after(1500, self._finalize_webui_stop)

    def _finalize_webui_stop(self) -> None:
        if is_process_running(self.webui_process):
            assert self.webui_process is not None
            self.webui_process.kill()
            self._log("WebUI 未及时退出，已强制结束。")
        self.webui_status_var.set("WebUI：未启动")

    def open_webui(self) -> None:
        host = (self.host_var.get().strip() or "127.0.0.1").strip("[]")
        port = self.port_var.get().strip() or "8000"
        browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        url = f"http://{browser_host}:{port}"
        webbrowser.open(url)
        self._log(f"已打开浏览器：{url}")

    def _save_settings_silently(self) -> None:
        try:
            settings = self._collect_settings()
            save_settings(settings)
            self.settings = settings
        except OSError as exc:
            self._log(f"配置保存失败：{exc}")

    def _poll_processes(self) -> None:
        if self.terminal_process is not None:
            code = self.terminal_process.poll()
            if code is None:
                self.terminal_status_var.set(f"终端：运行中 PID {self.terminal_process.pid}")
            else:
                self.terminal_status_var.set(f"终端：已退出，退出码 {code}")
                self.terminal_process = None

        if self.webui_process is not None:
            code = self.webui_process.poll()
            if code is None:
                self.webui_status_var.set(f"WebUI：运行中 PID {self.webui_process.pid}")
            else:
                self.webui_status_var.set(f"WebUI：已退出，退出码 {code}")
                self.webui_process = None
        self.after(1200, self._poll_processes)

    def _log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] {message}\n")

    def _post_ui(self, callback) -> None:
        self.ui_queue.put(callback)

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                callback = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            callback()
        self.after(100, self._drain_ui_queue)

    def _drain_log_queue(self) -> None:
        drained = False
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            drained = True
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message)
            self.log_text.configure(state="disabled")
        if drained:
            self.log_text.see("end")
        self.after(100, self._drain_log_queue)


def main() -> None:
    app = XenonLauncher()
    app.mainloop()


if __name__ == "__main__":
    main()
