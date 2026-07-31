"""
Xenon 自重启工具模块

提供 restart_self() 工具，允许 Xenon 在运行时触发自身重启。
适用于升级后、配置变更后等需要重启生效的场景。
完全内聚，不依赖外部文件。

工作方式:
    1. restart_self() 生成一个独立的 Python 进程 (DETACHED_PROCESS)
    2. 子进程通过环境变量接收参数，监视当前 PID 等待其退出
    3. 当前进程退出后，子进程启动新的 Xenon 实例

使用方式:
    load_module("restart_handler")
    result = restart_handler_RestartToolManager_restart_self(reason="upgrade")
"""
import os
import sys
import subprocess
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _find_project_root() -> Optional[Path]:
    """从当前文件位置向上查找项目根目录（含 Xenon.py）"""
    current = Path(__file__).resolve().parent.parent  # Tools/../ = 项目根
    if (current / "Xenon.py").exists():
        return current
    # 兜底: 从 sys.argv[0] 找
    if sys.argv and sys.argv[0]:
        candidate = Path(sys.argv[0]).resolve().parent
        if (candidate / "Xenon.py").exists():
            return candidate
    return None


_PROJECT_ROOT = _find_project_root()

# ── 模块级重启信号 ──
# cli_runtime.py 在每次 agent.chat() 后检查此标志
_restart_requested = False


def _signal_restart() -> None:
    """标记重启请求（供 cli_runtime 检查）"""
    global _restart_requested
    _restart_requested = True


def _clear_restart_signal() -> None:
    """清除重启信号"""
    global _restart_requested
    _restart_requested = False


class RestartToolManager:
    """Xenon 自重启工具管理器

    调用 restart_self() 可触发 Xenon 进程重启。
    工作方式: 生成一个后台辅助进程，等当前进程退出后启动新实例。

    使用方式:
        load_module("restart_handler")
        result = restart_handler_RestartToolManager_restart_self(reason="upgrade")
    """

    # ------------------------------------------------------------------ #
    #  内部辅助
    # ------------------------------------------------------------------ #

    # ── 内联重启辅助代码（替代外部 restart_helper.py）──
    _RESTART_HELPER_CODE = r'''"""Xenon 内联重启辅助进程（由 restart_handler.py 生成）"""
import os, sys, time, subprocess
from pathlib import Path

def _is_process_alive(pid: int) -> bool:
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x400000, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        else:
            os.kill(pid, 0)
            return True
    except (OSError, AttributeError, ImportError):
        return False

def main():
    old_pid = int(os.environ["RESTART_OLD_PID"])
    project_root = Path(os.environ["RESTART_PROJECT_ROOT"])
    venv_python = Path(os.environ["RESTART_VENV_PYTHON"])
    reason = os.environ.get("RESTART_REASON", "manual")
    target_script = os.environ["RESTART_TARGET_SCRIPT"]
    wait_start = time.time()
    timeout = 60
    while _is_process_alive(old_pid):
        elapsed = time.time() - wait_start
        if elapsed > timeout:
            break
        time.sleep(1)
    target_path = project_root / target_script
    if not target_path.exists():
        target_path = project_root / "Xenon.py"
        if not target_path.exists():
            sys.exit(1)
    cmd = [str(venv_python), str(target_path)]
    kwargs = {"cwd": str(project_root), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kwargs)
    except Exception:
        sys.exit(1)

if __name__ == "__main__":
    main()
'''


    @staticmethod
    def _get_venv_python() -> Optional[Path]:
        """获取虚拟环境的 Python 路径"""
        if not _PROJECT_ROOT:
            return None
        venv_dir = _PROJECT_ROOT / "venv"
        if os.name == "nt":
            return venv_dir / "Scripts" / "python.exe"
        return venv_dir / "bin" / "python"

    # ------------------------------------------------------------------ #
    #  外部接口
    # ------------------------------------------------------------------ #

    def check_feasibility(self) -> dict:
        """检查重启是否可行（内联模式，不依赖外部文件）

        Returns
        -------
        dict
            {
                "success": True,
                "feasible": True/False,      ← 是否具备重启条件
                "issues": [...],             ← 如果有问题，列出原因
                "venv_exists": True/False,
                "current_pid": 12345,
                "project_root": "D:\\Xenon\\agent_Xenon",
                "venv_python": "D:\\Xenon\\agent_Xenon\\venv\\Scripts\\python.exe",
            }
        """
        venv_python = self._get_venv_python()

        issues = []
        if not _PROJECT_ROOT:
            issues.append("无法确定项目根目录 (未找到 Xenon.py)")
        if not venv_python or not venv_python.exists():
            issues.append(f"虚拟环境 Python 不存在: {venv_python}")

        return {
            "success": True,
            "feasible": len(issues) == 0,
            "issues": issues,
            "venv_exists": venv_python.exists() if venv_python else False,
            "current_pid": os.getpid(),
            "project_root": str(_PROJECT_ROOT) if _PROJECT_ROOT else None,
            "venv_python": str(venv_python) if venv_python else None,
        }

    @staticmethod
    def _detect_runtime_mode() -> str:
        """自动检测当前运行模式: 'webui' 或 'cli'

        检测优先级:
          1. 环境变量 XENON_RUNTIME_MODE（最可靠，由 webui/main.py 在启动时设置）
          2. 通过 psutil 扫描所有 Python 进程，查找 webui/main.py（兜底）
        """
        # 优先级1: 环境变量（由 webui/main.py 在 import 阶段设置）
        env_mode = os.environ.get("XENON_RUNTIME_MODE")
        if env_mode in ("webui", "cli"):
            return env_mode

        # 优先级2: 扫描所有 Python 进程
        try:
            import psutil
            for proc in psutil.process_iter(["pid", "cmdline"]):
                try:
                    cmdline = proc.info.get("cmdline")
                    if not cmdline:
                        continue
                    cmd_str = " ".join(cmdline)
                    if "webui" in cmd_str and "main.py" in cmd_str:
                        return "webui"
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass
        return "cli"

    def restart_self(self, reason: str = "manual") -> dict:
        """触发 Xenon 自重启（内联模式，不依赖外部文件）

        当前进程会退出，并由内联辅助进程启动新实例。
        **调用后当前对话将结束，新窗口中将启动新实例。**

        Parameters
        ----------
        reason : str, optional
            重启原因描述，会记录在 restart.log 中。
            例如 "upgrade"（升级后重启）、"config_change"（配置变更后重启）。

        Returns
        -------
        dict
            正常:
                {
                    "success": True,
                    "message": "重启已触发，即将退出当前进程",
                    "old_pid": 12345,
                    "reason": "upgrade",
                    "restarting": True,
                }
            失败:
                {
                    "success": False,
                    "error": "错误描述",
                }
        """
        # ── 前置校验 ──
        venv_python = self._get_venv_python()

        errors = []
        if not _PROJECT_ROOT:
            errors.append("无法确定项目根目录")
        if not venv_python or not venv_python.exists():
            errors.append(f"虚拟环境 Python 不存在: {venv_python}")

        if errors:
            error_msg = "; ".join(errors)
            logger.error(f"重启失败: {error_msg}")
            return {"success": False, "error": error_msg}

        # ── 检测运行模式 ──
        mode = self._detect_runtime_mode()
        target_script = "webui/main.py" if mode == "webui" else "Xenon.py"

        current_pid = os.getpid()
        logger.info(f"触发自重启 (PID {current_pid}, 模式: {mode}, 原因: {reason})")

        # ── 通过环境变量传参，启动内联辅助进程 ──
        try:
            env = os.environ.copy()
            env["RESTART_OLD_PID"] = str(current_pid)
            env["RESTART_PROJECT_ROOT"] = str(_PROJECT_ROOT)
            env["RESTART_VENV_PYTHON"] = str(venv_python)
            env["RESTART_REASON"] = reason
            env["RESTART_TARGET_SCRIPT"] = target_script

            helper_args = [
                str(sys.executable),
                "-c",
                self._RESTART_HELPER_CODE,
            ]

            if os.name == "nt":
                # DETACHED_PROCESS = 0x00000008: 新进程不依附于当前控制台
                subprocess.Popen(
                    helper_args,
                    cwd=str(_PROJECT_ROOT),
                    env=env,
                    creationflags=subprocess.DETACHED_PROCESS,
                )
            else:
                subprocess.Popen(
                    helper_args,
                    cwd=str(_PROJECT_ROOT),
                    env=env,
                    start_new_session=True,
                )

            logger.info(f"内联重启辅助进程已启动，等待当前进程退出")
        except Exception as exc:
            error_msg = f"启动重启辅助进程失败: {exc}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg}

        # ── WebUI 模式：直接退出（无 cli_runtime 信号机制）──
        if mode == "webui":
            logger.info("WebUI 模式：强制退出当前进程")
            import threading
            threading.Thread(target=lambda: os._exit(0), daemon=True).start()
            return {
                "success": True,
                "message": "WebUI 重启已触发，进程即将退出，新进程将在旧进程退出后启动",
                "old_pid": current_pid,
                "reason": reason,
                "target": target_script,
                "restarting": True,
            }

        # ── CLI 模式：设置模块级信号 ──
        _signal_restart()

        return {
            "success": True,
            "message": "重启已触发，即将退出当前进程",
            "old_pid": current_pid,
            "reason": reason,
            "target": target_script,
            "restarting": True,
        }
