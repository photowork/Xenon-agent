"""
Xenon 心跳看门狗 (Heartbeat Watchdog)

独立进程，监控心跳文件，检测到超时时自动重启 Xenon。

用法:
    python xenon_core/heartbeat_watchdog.py [project_root] [--once]

    --once       检测一次就退出（适合 cron / 计划任务）
    （无 --once）持续监控，循环检测

工作原理:
    1. 读取 logs/heartbeat.json 中的 timestamp
    2. 如果距上次心跳超过 XENON_HEARTBEAT_STALE_TIMEOUT 秒 → 判定死亡
    3. 调用 restart_helper.py 重启 Xenon
    4. 记录所有操作到 logs/watchdog.log

环境变量:
    XENON_HEARTBEAT_STALE_TIMEOUT: 判定死亡超时秒数，默认 30
    XENON_WATCHDOG_CHECK_INTERVAL: 检测间隔秒数，默认 10
"""
from __future__ import annotations

import json
import os
import sys
import time
import subprocess
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# ── 配置 ──
STALE_TIMEOUT = int(os.environ.get("XENON_HEARTBEAT_STALE_TIMEOUT", "30"))
CHECK_INTERVAL = int(os.environ.get("XENON_WATCHDOG_CHECK_INTERVAL", "10"))

# ── 日志 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("watchdog")


def find_project_root() -> Optional[Path]:
    """推断项目根目录"""
    # 优先用命令行参数
    if len(sys.argv) > 1 and sys.argv[1] not in ("--once", "--help", "-h"):
        candidate = Path(sys.argv[1])
        if candidate.exists():
            return candidate
    
    # 从脚本位置推断: xenon_core/../ = 项目根
    script_dir = Path(__file__).resolve().parent.parent
    if (script_dir / "Xenon.py").exists():
        return script_dir
    
    # 兜底：当前工作目录
    cwd = Path.cwd()
    if (cwd / "Xenon.py").exists():
        return cwd
    
    return None


def read_heartbeat(project_root: Path) -> Optional[dict]:
    """读取心跳文件"""
    hb_file = project_root / "logs" / "heartbeat.json"
    if not hb_file.exists():
        return None
    
    try:
        with open(hb_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"无法读取心跳文件: {e}")
        return None


def is_stale(heartbeat: dict, timeout: int = STALE_TIMEOUT) -> bool:
    """判断心跳是否超时"""
    ts_str = heartbeat.get("timestamp")
    if not ts_str:
        return True
    
    try:
        last_beat = datetime.fromisoformat(ts_str)
    except ValueError:
        return True
    
    age = (datetime.now(timezone.utc).replace(tzinfo=None) - last_beat.replace(tzinfo=None)).total_seconds()
    return age > timeout


def kill_stale_process(pid: int) -> bool:
    """强制终止超时进程"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            os.kill(pid, 9)  # SIGKILL
        logger.info(f"已终止超时进程 PID {pid}")
        return True
    except Exception as e:
        logger.error(f"无法终止进程 PID {pid}: {e}")
        return False


def restart_xenon(project_root: Path) -> bool:
    """通过 restart_helper 重启 Xenon"""
    helper = project_root / "restart_helper.py"
    venv_python = _find_venv_python(project_root)
    
    if not helper.exists():
        logger.error(f"restart_helper 不存在: {helper}")
        return False
    if not venv_python or not venv_python.exists():
        logger.error(f"venv python 不存在: {venv_python}")
        return False
    
    # 检测运行模式
    mode = _detect_mode(project_root)
    target_script = "webui/main.py" if mode == "webui" else "Xenon.py"
    
    try:
        # restart_helper 需要一个 old_pid 来等待，但此时进程已经死了
        # 传一个无效 PID (0)，helper 会立即检测到进程不存在并启动新进程
        helper_args = [
            str(sys.executable),
            str(helper),
            "0",  # old_pid — 传 0，helper 发现进程不存在直接启动
            str(project_root),
            str(venv_python),
            "watchdog",
            target_script,
        ]
        
        if os.name == "nt":
            subprocess.Popen(
                helper_args,
                cwd=str(project_root),
                creationflags=subprocess.DETACHED_PROCESS,
            )
        else:
            subprocess.Popen(
                helper_args,
                cwd=str(project_root),
                start_new_session=True,
            )
        
        logger.info(f"已触发重启 (mode={mode}, target={target_script})")
        return True
    except Exception as e:
        logger.error(f"重启失败: {e}")
        return False


def _find_venv_python(project_root: Path) -> Optional[Path]:
    """查找 venv Python"""
    if os.name == "nt":
        venv_python = project_root / "venv" / "Scripts" / "python.exe"
    else:
        venv_python = project_root / "venv" / "bin" / "python"
    return venv_python if venv_python.exists() else None


def _detect_mode(project_root: Path) -> str:
    """通过心跳文件检测运行模式"""
    hb = read_heartbeat(project_root)
    if hb:
        mode = hb.get("mode", "")
        if mode in ("webui", "cli"):
            return mode
    
    # 兜底：检查是否有 webui 进程
    try:
        import psutil
        for proc in psutil.process_iter(["cmdline"]):
            try:
                cmd_str = " ".join(proc.info.get("cmdline") or [])
                if "webui" in cmd_str and "main.py" in cmd_str:
                    return "webui"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    
    return "cli"


def log_event(project_root: Path, event: str, details: str = "") -> None:
    """记录看门狗事件到日志"""
    log_dir = project_root / "logs"
    log_dir.mkdir(exist_ok=True)
    try:
        with open(log_dir / "watchdog.log", "a", encoding="utf-8") as f:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"{ts} | {event}"
            if details:
                line += f" | {details}"
            f.write(line + "\n")
    except OSError:
        pass


def check_once(project_root: Path) -> bool:
    """单次检测：返回 True 表示进程健康，False 表示已触发重启"""
    hb = read_heartbeat(project_root)
    
    if hb is None:
        logger.warning("心跳文件不存在，进程可能未启动或已死")
        log_event(project_root, "NO_HEARTBEAT_FILE", "心跳文件缺失")
        return False
    
    pid = hb.get("pid", 0)
    status = hb.get("status", "")
    
    if status == "stopped":
        logger.info("心跳状态为 'stopped'，进程已正常退出")
        return True  # 正常退出，不需要重启
    
    if is_stale(hb):
        age = "N/A"
        ts_str = hb.get("timestamp", "")
        if ts_str:
            try:
                age = str(int((datetime.now(timezone.utc).replace(tzinfo=None) - 
                               datetime.fromisoformat(ts_str).replace(tzinfo=None)).total_seconds()))
            except Exception:
                pass
        
        logger.warning(f"心跳超时！PID={pid}, 上次心跳距今 {age}s")
        log_event(project_root, "STALE_HEARTBEAT", f"PID={pid}, age={age}s")
        
        # 尝试杀死旧进程（可能还在但挂起了）
        if pid and pid > 0:
            kill_stale_process(pid)
        
        # 重启
        if restart_xenon(project_root):
            log_event(project_root, "RESTART_TRIGGERED", f"from PID={pid}")
            return False
        else:
            log_event(project_root, "RESTART_FAILED", "restart_helper 调用失败")
            return False
    
    return True


def watch_loop(project_root: Path) -> None:
    """持续监控循环"""
    logger.info(f"看门狗启动 (项目: {project_root}, 超时: {STALE_TIMEOUT}s, 检测间隔: {CHECK_INTERVAL}s)")
    log_event(project_root, "WATCHDOG_STARTED", f"timeout={STALE_TIMEOUT}s, interval={CHECK_INTERVAL}s")
    
    consecutive_failures = 0
    
    while True:
        try:
            healthy = check_once(project_root)
            if healthy:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures > 3:
                    logger.critical(f"连续 {consecutive_failures} 次重启失败，看门狗暂停 5 分钟")
                    log_event(project_root, "WATCHDOG_PAUSED", f"consecutive_failures={consecutive_failures}")
                    time.sleep(300)
                    consecutive_failures = 0
        except Exception as e:
            logger.error(f"检测异常: {e}")
        
        time.sleep(CHECK_INTERVAL)


# ═══════════════════════════════════════════════════════════════════ #
#  main
# ═══════════════════════════════════════════════════════════════════ #

def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return
    
    project_root = find_project_root()
    if project_root is None:
        print("错误: 无法确定项目根目录。请在命令行指定: python heartbeat_watchdog.py <project_root>")
        sys.exit(1)
    
    once = "--once" in sys.argv
    
    if once:
        logger.info("单次检测模式")
        healthy = check_once(project_root)
        if healthy:
            logger.info("心跳正常 ✓")
        else:
            logger.warning("心跳异常，已尝试重启")
            sys.exit(1)
    else:
        watch_loop(project_root)


if __name__ == "__main__":
    main()
