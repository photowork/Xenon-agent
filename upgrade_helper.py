"""
Xenon 升级助手 - 程序升级导出/导入工具
支持将程序主要部分导出到指定文件夹，或从指定文件夹导入覆盖现有程序
"""

import os
import sys
import shutil
import json
import time
import threading
import tkinter as tk
from tkinter import filedialog, ttk
from datetime import datetime
from pathlib import Path


# ============================================================
# 配置清单
# ============================================================

# 项目根目录（自动检测：本文件所在目录）
PROJECT_ROOT = Path(__file__).parent.resolve()

# 导出包含清单
EXPORT_INCLUDE = [
    "xenon_core",          # 目录
    "webui",               # 目录
    "Tools",               # 目录
    "prompts",             # 目录
    "timer_app",           # 目录
    "Xenon.py",            # 文件
    "launcher.py",         # 文件
    "deepseekconfig.py",   # 文件
    "requirements.txt",    # 文件
    "start_Xenon.vbs",     # 文件
    "start_Terminal.bat",  # 文件
    "xenon_logo.ico",      # 文件
    "upgrade_helper.py",   # 文件
]

# 排除清单（这些路径在导出/导入时跳过）
EXPORT_EXCLUDE = [
    "config",              # 含 API key，新安装自动创建
    "webui/sessions",      # 会话数据
]

# 通用排除模式（文件名/目录名匹配）
EXCLUDE_PATTERNS = [
    "__pycache__",
    ".pyc",
    ".git",
]

# 元数据文件名
MANIFEST_FILE = "upgrade_manifest.json"


# ============================================================
# 核心逻辑
# ============================================================

class UpgradeHelper:
    """升级助手核心逻辑"""

    def __init__(self, project_root=None, log_callback=None):
        self.project_root = Path(project_root) if project_root else PROJECT_ROOT
        self.log_callback = log_callback or self._default_log

    def _default_log(self, msg):
        print(msg)

    def log(self, msg):
        if self.log_callback:
            self.log_callback(msg)

    def _should_exclude(self, rel_path: str) -> bool:
        """检查相对路径是否应该被排除"""
        # 检查显式排除清单
        for exc in EXPORT_EXCLUDE:
            if rel_path == exc or rel_path.startswith(exc.replace("/", os.sep) + os.sep):
                return True
        # 检查通用排除模式
        parts = rel_path.replace("\\", "/")
        for pattern in EXCLUDE_PATTERNS:
            if pattern in parts:
                return True
        return False

    def _collect_files(self) -> dict:
        """
        收集所有需要导出的文件
        返回: {相对路径: 绝对路径} 的字典
        """
        files = {}

        for item in EXPORT_INCLUDE:
            abs_path = self.project_root / item
            if not abs_path.exists():
                self.log(f"  [跳过] 不存在: {item}")
                continue

            if abs_path.is_file():
                if not self._should_exclude(item):
                    files[item] = str(abs_path)
                else:
                    self.log(f"  [排除] {item}")
            elif abs_path.is_dir():
                for root, dirs, filenames in os.walk(str(abs_path)):
                    # 先过滤目录（避免遍历被排除的子目录）
                    rel_root = os.path.relpath(root, str(self.project_root))
                    # 过滤目录名
                    dirs[:] = [
                        d for d in dirs
                        if not self._should_exclude(os.path.join(rel_root, d).replace("\\", "/"))
                        and d not in EXCLUDE_PATTERNS
                    ]
                    for fname in filenames:
                        rel_file = os.path.relpath(
                            os.path.join(root, fname), str(self.project_root)
                        ).replace("\\", "/")
                        # 跳过 .pyc 等模式
                        if any(pat in rel_file for pat in EXCLUDE_PATTERNS):
                            continue
                        if not self._should_exclude(rel_file):
                            files[rel_file] = os.path.join(root, fname)

        return files

    def export_to(self, target_dir: str) -> bool:
        """
        导出程序主要部分到目标文件夹
        """
        target = Path(target_dir)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.log(f"[错误] 无法创建目标文件夹: {e}")
            return False

        self.log(f"正在收集文件...")
        files = self._collect_files()
        self.log(f"共找到 {len(files)} 个文件")

        # 构建清单
        manifest = {
            "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_root": str(self.project_root),
            "file_count": len(files),
            "files": {},
        }

        success = 0
        failed = 0
        for rel_path, abs_path in files.items():
            dest = target / rel_path
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(abs_path, str(dest))
                # 记录文件大小
                manifest["files"][rel_path] = {
                    "size": os.path.getsize(abs_path),
                }
                success += 1
            except Exception as e:
                self.log(f"  [失败] {rel_path}: {e}")
                failed += 1

        # 写入清单
        manifest_path = target / MANIFEST_FILE
        try:
            with open(str(manifest_path), "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"[警告] 无法写入清单文件: {e}")

        self.log(f"导出完成: 成功 {success} 个, 失败 {failed} 个")
        self.log(f"目标位置: {target}")
        return failed == 0

    def import_from(self, source_dir: str) -> bool:
        """
        从源文件夹导入程序主要部分，覆盖当前项目
        """
        source = Path(source_dir)
        manifest_path = source / MANIFEST_FILE

        # 读取清单（如果有）
        manifest = None
        if manifest_path.exists():
            try:
                with open(str(manifest_path), "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                self.log(f"检测到升级清单，导出时间: {manifest.get('export_time', '未知')}")
            except Exception as e:
                self.log(f"[警告] 清单读取失败，将直接复制: {e}")

        # 收集源文件夹中的文件（排除清单文件自身）
        files = {}
        if manifest and manifest.get("files"):
            # 使用清单中的文件列表
            for rel_path in manifest["files"]:
                abs_path = source / rel_path
                if abs_path.exists():
                    files[rel_path] = str(abs_path)
                else:
                    self.log(f"  [跳过] 清单中文件不存在: {rel_path}")
        else:
            # 没有清单，扫描源文件夹
            self.log("未找到清单，扫描源文件夹所有文件...")
            for root, dirs, filenames in os.walk(str(source)):
                # 过滤目录
                dirs[:] = [
                    d for d in dirs
                    if d not in EXCLUDE_PATTERNS
                    and d != ".git"
                ]
                for fname in filenames:
                    if fname == MANIFEST_FILE:
                        continue
                    if any(pat in fname for pat in EXCLUDE_PATTERNS):
                        continue
                    rel_file = os.path.relpath(
                        os.path.join(root, fname), str(source)
                    ).replace("\\", "/")
                    files[rel_file] = os.path.join(root, fname)

        self.log(f"共找到 {len(files)} 个文件待导入")

        # 应用排除规则
        filtered = {}
        skipped = []
        for rel_path, abs_path in files.items():
            if self._should_exclude(rel_path):
                skipped.append(rel_path)
            else:
                filtered[rel_path] = abs_path

        if skipped:
            self.log(f"排除 {len(skipped)} 个敏感/跳过文件:")
            for s in skipped:
                self.log(f"  [排除] {s}")

        # 执行导入
        success = 0
        failed = 0
        for rel_path, abs_path in filtered.items():
            dest = self.project_root / rel_path
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(abs_path, str(dest))
                success += 1
            except Exception as e:
                self.log(f"  [失败] {rel_path}: {e}")
                failed += 1

        self.log(f"导入完成: 成功 {success} 个, 失败 {failed} 个")
        return failed == 0


# ============================================================
# GUI 界面
# ============================================================

class UpgradeHelperGUI:
    """升级助手 tkinter GUI"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Xenon 升级助手")
        self.root.geometry("640x520")
        self.root.resizable(True, True)
        self.root.minsize(500, 400)

        # 设置图标（如果有）
        icon_path = PROJECT_ROOT / "xenon_logo.ico"
        if icon_path.exists():
            try:
                self.root.iconbitmap(str(icon_path))
            except Exception:
                pass

        self.helper = UpgradeHelper(log_callback=self._append_log)
        self._build_ui()

    def _build_ui(self):
        """构建界面"""
        # 主框架
        main_frame = ttk.Frame(self.root, padding=15)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 标题
        title_label = ttk.Label(
            main_frame,
            text="Xenon 升级助手",
            font=("Microsoft YaHei", 16, "bold")
        )
        title_label.pack(pady=(0, 5))

        # 项目路径显示
        path_frame = ttk.Frame(main_frame)
        path_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(path_frame, text="项目路径:", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        ttk.Label(
            path_frame,
            text=str(PROJECT_ROOT),
            font=("Consolas", 9),
            foreground="gray"
        ).pack(side=tk.LEFT, padx=(5, 0))

        # 操作按钮区域
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(0, 10))

        # 导出按钮
        self.export_btn = ttk.Button(
            btn_frame,
            text="📦  导出到文件夹",
            command=self._on_export,
            width=25
        )
        self.export_btn.pack(side=tk.LEFT, padx=(0, 10), ipady=6)

        # 导入按钮
        self.import_btn = ttk.Button(
            btn_frame,
            text="📥  从文件夹导入",
            command=self._on_import,
            width=25
        )
        self.import_btn.pack(side=tk.LEFT, ipady=6)

        # 分隔线
        ttk.Separator(main_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=5)

        # 日志区域标签
        log_label = ttk.Label(main_frame, text="操作日志", font=("Microsoft YaHei", 9, "bold"))
        log_label.pack(anchor=tk.W, pady=(5, 2))

        # 日志文本框 + 滚动条
        log_frame = ttk.Frame(main_frame)
        log_frame.pack(fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(log_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.log_text = tk.Text(
            log_frame,
            font=("Consolas", 9),
            wrap=tk.WORD,
            state=tk.DISABLED,
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="white",
            yscrollcommand=scrollbar.set,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.log_text.yview)

        # 底部状态栏
        self.status_var = tk.StringVar(value="就绪")
        status_bar = ttk.Label(
            self.root,
            textvariable=self.status_var,
            relief=tk.SUNKEN,
            anchor=tk.W,
            font=("Microsoft YaHei", 8)
        )
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

    def _append_log(self, msg: str):
        """线程安全地追加日志"""
        def _do():
            self.log_text.config(state=tk.NORMAL)
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.log_text.insert(tk.END, f"[{timestamp}] {msg}\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        # 如果在主线程就直接执行，否则用 after
        if threading.current_thread() is threading.main_thread():
            _do()
        else:
            self.root.after(0, _do)

    def _set_buttons_state(self, enabled: bool):
        """启用/禁用按钮"""
        state = tk.NORMAL if enabled else tk.DISABLED
        self.export_btn.config(state=state)
        self.import_btn.config(state=state)

    def _on_export(self):
        """导出按钮点击"""
        target = filedialog.askdirectory(title="选择导出目标文件夹")
        if not target:
            return

        self._set_buttons_state(False)
        self.status_var.set("正在导出...")

        def _worker():
            try:
                self.helper.export_to(target)
            except Exception as e:
                self._append_log(f"[错误] 导出失败: {e}")
            finally:
                self.root.after(0, lambda: self._set_buttons_state(True))
                self.root.after(0, lambda: self.status_var.set("导出完成"))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_import(self):
        """导入按钮点击"""
        source = filedialog.askdirectory(title="选择导入源文件夹")
        if not source:
            return

        # 检查源文件夹是否有效
        manifest_path = Path(source) / MANIFEST_FILE
        if not manifest_path.exists():
            # 没有清单，给个提示但不阻止
            self._append_log("[提示] 未检测到升级清单文件，将复制源文件夹中的所有文件")

        self._set_buttons_state(False)
        self.status_var.set("正在导入...")

        def _worker():
            try:
                self.helper.import_from(source)
            except Exception as e:
                self._append_log(f"[错误] 导入失败: {e}")
            finally:
                self.root.after(0, lambda: self._set_buttons_state(True))
                self.root.after(0, lambda: self.status_var.set("导入完成"))

        threading.Thread(target=_worker, daemon=True).start()

    def run(self):
        """启动 GUI"""
        self.root.mainloop()


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    app = UpgradeHelperGUI()
    app.run()
