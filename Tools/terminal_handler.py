#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Terminal/Shell 操作工具 (优化版)
支持执行命令行命令，包括 cmd、PowerShell 和其他终端命令
修复了乱码、进程泄露和命令注入问题
"""

import json
import sys
import subprocess
import os
import platform
import base64
import shutil
import signal
from pathlib import Path
from typing import Dict, Any, Optional, Union

class TerminalHandler:
    def __init__(self, sandbox_context=None):
        # Phase 4: 支持沙箱隔离执行
        self.sandbox_context = sandbox_context
        if sandbox_context is not None:
            self.current_directory = str(sandbox_context.sandbox_dir)
        else:
            self.current_directory = os.getcwd()
        self.platform_system = platform.system().lower()

    def _terminate_process_tree(self, process: subprocess.Popen) -> None:
        """尽量终止整个进程树，避免 shell 包裹命令超时后留下子进程。"""
        if not process:
            return

        try:
            if self.platform_system == 'windows':
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False
                )
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _decode_output(self, byte_data: bytes) -> str:
        """
        智能解码输出，解决乱码问题
        优先尝试 UTF-8，失败后尝试 GBK (Windows默认)，最后使用 replace 忽略错误
        """
        if not byte_data:
            return ""
        
        # 1. 优先尝试 UTF-8 (现代工具和 Windows 新版控制台常用)
        try:
            return byte_data.decode('utf-8')
        except UnicodeDecodeError:
            pass
        
        # 2. 尝试 GBK (中文 Windows 传统默认)
        try:
            return byte_data.decode('gbk')
        except UnicodeDecodeError:
            pass
            
        # 3. 最终回退：使用系统默认编码，忽略无法解码的字符
        try:
            return byte_data.decode(sys.getdefaultencoding(), errors='replace')
        except:
            return byte_data.decode('latin1', errors='replace')

    def _execute_internal(self, exec_cmd: Union[str, list], timeout: int, working_dir: Optional[str], is_powershell: bool = False) -> Dict[str, Any]:
        """
        核心执行逻辑，统一处理超时和流读取
        """
        start_time = os.times().elapsed if hasattr(os.times(), 'elapsed') else 0 # Python 3.7+ process_time
        
        # 确定工作目录
        exec_working_dir = str(Path(working_dir).resolve()) if working_dir else self.current_directory
        
        # 设置环境变量
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        # 强制某些 Windows 工具输出 UTF-8 (可选，不仅限于 Python)
        if self.platform_system == 'windows':
            env['PYTHONUTF8'] = '1'

        popen_kwargs = {}
        if self.platform_system == 'windows':
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        # Windows + shell=True 多行命令修复
        # cmd.exe 会把换行符解析为命令分隔符，导致多行 python -c "..." 等命令被截断
        # 方案：检测 python -c 多行命令 → Base64 编码代码块 → 单行执行
        _tmp_script_file = None
        if self.platform_system == 'windows' and isinstance(exec_cmd, str) and '\n' in exec_cmd:
            import re as _re
            # 用 DOTALL 让 . 匹配换行，匹配到结束引号为止，保留代码中的换行符
            py_match = _re.match(
                r'^python\s+-c\s+(["\'])(.*)\1',
                exec_cmd,
                _re.DOTALL
            )
            if py_match:
                code = py_match.group(2)
                import base64
                encoded = base64.b64encode(code.encode('utf-8')).decode('ascii')
                exec_cmd = f'python -c "import base64;exec(base64.b64decode(\'{encoded}\'))"'
            else:
                # 非 python 多行命令：写入临时 .py 文件（换行符在 .py 文件中合法）
                import tempfile, uuid
                _tmp_script_file = os.path.join(
                    tempfile.gettempdir(),
                    f"xenon_tmp_{uuid.uuid4().hex[:8]}.py"
                )
                try:
                    with open(_tmp_script_file, 'w', encoding='utf-8') as _f:
                        _f.write(exec_cmd + '\n')
                    exec_cmd = f'python "{_tmp_script_file}"'
                except Exception:
                    if os.path.exists(_tmp_script_file):
                        try: os.remove(_tmp_script_file)
                        except: pass
                    _tmp_script_file = None

        process = None
        try:
            # 使用 Popen 以便手动控制流和超时
            # 注意：不指定 encoding 和 text，直接读取 bytes
            process = subprocess.Popen(
                exec_cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE, # 提供 stdin 防止部分命令挂起等待输入
                cwd=exec_working_dir,
                env=env,
                **popen_kwargs
            )
            
            # 等待进程结束，设置超时
            # communicate 返回的是 bytes
            stdout_bytes, stderr_bytes = process.communicate(timeout=timeout)
            
            # 智能解码
            stdout = self._decode_output(stdout_bytes)
            stderr = self._decode_output(stderr_bytes)
            
            returncode = process.returncode
            
            # 计算耗时
            end_time = os.times().elapsed if hasattr(os.times(), 'elapsed') else 0
            
            return {
                "success": True,
                "exit_code": returncode,
                "stdout": stdout,
                "stderr": stderr,
                "execution_time": round(end_time - start_time, 2),
                "working_dir": exec_working_dir
            }

        except subprocess.TimeoutExpired:
            # 关键修复：超时必须杀死进程
            if process:
                try:
                    self._terminate_process_tree(process)
                    process.communicate() # 清理缓冲区避免僵尸进程
                except Exception:
                    pass
            
            # 返回超时错误信息（解码尽量尝试）
            err_msg = f"命令执行超时 ({timeout}秒)"
            # 尝试读取超时前产生的输出（如果有）
            # 注意：TimeoutExpired 异常对象中可能包含部分 output，但比较复杂，这里简化处理
            return {
                "success": False,
                "error": err_msg,
                "exit_code": -1,
                "stdout": "",
                "stderr": err_msg
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"执行异常: {str(e)}",
                "exit_code": -1
            }
            
        finally:
            # 清理多行命令创建的临时脚本文件
            if _tmp_script_file and os.path.exists(_tmp_script_file):
                try:
                    os.remove(_tmp_script_file)
                except Exception:
                    pass

    def _format_result(
        self,
        result: Dict[str, Any],
        command: str,
        command_type: str,
        max_output_lines: int = 160,
        max_output_chars: int = 20000,
    ) -> Dict[str, Any]:
        """统一格式化输出结果，支持行数和字符数截断"""
        if not result.get("success"):
            return {
                **result,
                "command": command,
                "message": f"{command_type} 执行失败: {result.get('error', 'Unknown error')}"
            }

        stdout_raw = result.get("stdout", "")
        stderr_raw = result.get("stderr", "")
        exit_code = result.get("exit_code", -1)
        
        stdout = self._clean_output(stdout_raw)
        stderr = self._clean_output(stderr_raw)
        
        # ---- 截断逻辑 ----
        truncated = False
        
        # 先按字符数截断
        if max_output_chars > 0 and len(stdout) > max_output_chars:
            truncated = True
            stdout = stdout[:max_output_chars] + f"\n... [截断: 输出超过 {max_output_chars} 字符限制]"
        
        if max_output_chars > 0 and len(stderr) > max_output_chars:
            stderr = stderr[:max_output_chars] + f"\n... [截断: 错误输出超过 {max_output_chars} 字符限制]"
        
        output_lines = stdout.split('\n') if stdout else []
        error_lines = stderr.split('\n') if stderr else []
        
        # 再按行数截断
        if max_output_lines > 0 and len(output_lines) > max_output_lines:
            truncated = True
            output_lines = output_lines[:max_output_lines]
            output_lines.append(f"... [截断: 输出超过 {max_output_lines} 行限制]")
        
        if max_output_lines > 0 and len(error_lines) > max_output_lines:
            error_lines = error_lines[:max_output_lines]
            error_lines.append(f"... [截断: 错误输出超过 {max_output_lines} 行限制]")
        
        command_succeeded = exit_code == 0
        status_msg = "成功" if command_succeeded else f"失败(退出码: {exit_code})"
        
        return {
            "success": command_succeeded,
            "command": command,
            "exit_code": exit_code,
            "output": output_lines,
            "errors": error_lines,
            "working_dir": result.get("working_dir"),
            "execution_time": result.get("execution_time", 0),
            "output_lines": len(output_lines),
            "error_lines": len(error_lines),
            "truncated": truncated,
            "message": f"{command_type}执行{status_msg} | 耗时: {result.get('execution_time', 0):.2f}秒"
        }

    def _clean_output(self, text: str) -> str:
        """清理输出文本：移除多余空白和换行符"""
        if not text:
            return ""
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        lines = text.split('\n')
        cleaned_lines = []
        prev_empty = False
        for line in lines:
            stripped = line.rstrip()
            is_empty = not stripped
            if is_empty:
                if not prev_empty and cleaned_lines:
                    prev_empty = True
                    cleaned_lines.append("")
            else:
                prev_empty = False
                if ' : ' in stripped or ' :' in stripped:
                    stripped = self._clean_table_line(stripped)
                cleaned_lines.append(stripped)
        while cleaned_lines and not cleaned_lines[-1]:
            cleaned_lines.pop()
        return '\n'.join(cleaned_lines)

    def _clean_table_line(self, line: str) -> str:
        """清理表格行中的多余空格"""
        import re
        match = re.match(r'^(\S.*?)\s+:\s*(.*)$', line)
        if match:
            key = match.group(1).rstrip()
            value = match.group(2).strip()
            return f"{key} : {value}" if value else f"{key} :"
        return line.rstrip()

    def execute_command(self, command: str, timeout: int = 300, working_dir: Optional[str] = None, non_interactive: bool = True, max_output_lines: int = 160, max_output_chars: int = 20000) -> Dict[str, Any]:
        """
        执行通用终端命令
        """
        exec_cmd = command
        
        # 简单的非交互处理
        if non_interactive and command.lower().endswith('.bat'):
             # echo. | command 确保有输入，防止 bat 暂停
            exec_cmd = f'echo. | {command}'

        raw_result = self._execute_internal(exec_cmd, timeout, working_dir)
        return self._format_result(raw_result, command, "命令", max_output_lines, max_output_chars)

    def execute_cmd_command(self, command: str, timeout: int = 300, working_dir: Optional[str] = None, non_interactive: bool = True, max_output_lines: int = 160, max_output_chars: int = 20000) -> Dict[str, Any]:
        """
        执行 CMD 命令 (Windows)
        """
        if self.platform_system != 'windows':
            return {
                "success": False,
                "error": "CMD 命令仅在 Windows 系统上可用",
                "command": command
            }

        exec_cmd = command
        if not command.strip().lower().startswith('cmd'):
            # 设置 Code Page 为 65001 (UTF-8) 以支持更好的一致性，
            # 虽然 _decode_output 会自动处理，但设置 CP65001 有助于某些内置命令输出 UTF8
            exec_cmd = f'cmd /c chcp 65001 >nul && {command}'
        
        if non_interactive and '.bat' in command.lower():
            exec_cmd = f'echo. | {exec_cmd}'

        raw_result = self._execute_internal(exec_cmd, timeout, working_dir)
        return self._format_result(raw_result, command, "CMD", max_output_lines, max_output_chars)

    def execute_powershell_command(self, command: str, timeout: int = 300, working_dir: Optional[str] = None, max_output_lines: int = 160, max_output_chars: int = 20000) -> Dict[str, Any]:
        """
        执行 PowerShell 命令
        使用 Base64 编码避免引号和转义地狱
        """
        powershell_exe = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell_exe:
            return {
                "success": False,
                "error": "未找到 powershell 或 pwsh 可执行文件",
                "command": command
            }

        # 构造 PowerShell 命令
        # 强制内部输出编码为 UTF-8
        ps_script = f"""
$ProgressPreference = 'SilentlyContinue'
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
{command}
"""
        # 必须使用 UTF-16LE 编码转换为 bytes，PowerShell -EncodedCommand 要求如此
        script_bytes = ps_script.strip().encode('utf-16-le')
        encoded_cmd = base64.b64encode(script_bytes).decode('ascii')
        
        # 构造最终命令
        # -NonInteractive 不显示交互式提示
        # -NoProfile 不加载用户配置文件(加快启动)
        exec_cmd = f'"{powershell_exe}" -NonInteractive -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded_cmd}'
        
        raw_result = self._execute_internal(exec_cmd, timeout, working_dir, is_powershell=True)
        return self._format_result(raw_result, command, "PowerShell", max_output_lines, max_output_chars)

    def get_system_info(self) -> Dict[str, Any]:
        try:
            return {
                "platform": platform.system(),
                "version": platform.version(),
                "release": platform.release(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "python_version": platform.python_version(),
                "current_directory": os.getcwd(),
                "success": True
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def check_command_exists(self, command: str) -> Dict[str, Any]:
        try:
            command = command.strip()
            if not command:
                return {"success": False, "error": "命令不能为空", "command": command}

            cmd = ["where", command] if self.platform_system == 'windows' else ["which", command]
            result = subprocess.run(cmd, shell=False, capture_output=True)
            
            if result.returncode == 0:
                path = self._decode_output(result.stdout).strip().splitlines()[0]
                return {"success": True, "exists": True, "path": path, "command": command}
            else:
                return {"success": True, "exists": False, "command": command}
        except Exception as e:
            return {"success": False, "error": str(e), "command": command}

# --- 保持原有的管理器和 Main 接口以兼容调用方 ---

class TerminalToolManager:
    def __init__(self):
        self.handler = TerminalHandler()

    def execute_command(self, command: str, timeout: int = 300, working_dir: Optional[str] = None, non_interactive: bool = True, max_output_lines: int = 160, max_output_chars: int = 20000) -> Dict[str, Any]:
        return self.handler.execute_command(command, timeout, working_dir, non_interactive, max_output_lines, max_output_chars)

    def execute_cmd_command(self, command: str, timeout: int = 300, working_dir: Optional[str] = None, non_interactive: bool = True, max_output_lines: int = 160, max_output_chars: int = 20000) -> Dict[str, Any]:
        return self.handler.execute_cmd_command(command, timeout, working_dir, non_interactive, max_output_lines, max_output_chars)

    def execute_powershell_command(self, command: str, timeout: int = 30, working_dir: Optional[str] = None, max_output_lines: int = 160, max_output_chars: int = 20000) -> Dict[str, Any]:
        return self.handler.execute_powershell_command(command, timeout, working_dir, max_output_lines, max_output_chars)
        
    def get_system_info(self) -> Dict[str, Any]:
        return self.handler.get_system_info()

    def check_command_exists(self, command: str) -> Dict[str, Any]:
        return self.handler.check_command_exists(command)

def create_terminal_tool_manager():
    return TerminalToolManager()

def main():
    # 简化 main 入口，保持原有参数解析逻辑
    import time # 导入 time 用于计算耗时，因为 os.times() 在某些平台精度不同
    
    if len(sys.argv) < 2:
        print(json.dumps({"success": False, "error": "缺少参数"}, ensure_ascii=False))
        sys.exit(1)

    action = sys.argv[1]
    manager = TerminalToolManager()

    try:
        if action in ["execute", "execute_cmd", "execute_powershell"]:
            if len(sys.argv) < 3:
                print(json.dumps({"success": False, "error": "缺少命令参数"}, ensure_ascii=False))
                sys.exit(1)
            
            cmd_arg = sys.argv[2]
            timeout_arg = int(sys.argv[3]) if len(sys.argv) > 3 else 300
            workdir_arg = sys.argv[4] if len(sys.argv) > 4 else None
            
            if action == "execute":
                res = manager.execute_command(cmd_arg, timeout_arg, workdir_arg)
            elif action == "execute_cmd":
                res = manager.execute_cmd_command(cmd_arg, timeout_arg, workdir_arg)
            else:
                res = manager.execute_powershell_command(cmd_arg, timeout_arg, workdir_arg)
            
            print(json.dumps(res, ensure_ascii=False, indent=2))

        elif action == "get_system_info":
            print(json.dumps(manager.get_system_info(), ensure_ascii=False, indent=2))
        
        elif action == "check_command":
            if len(sys.argv) < 3: raise ValueError("Missing command")
            print(json.dumps(manager.check_command_exists(sys.argv[2]), ensure_ascii=False, indent=2))
        
        else:
            print(json.dumps({"success": False, "error": f"未知操作: {action}"}, ensure_ascii=False))

    except Exception as e:
        print(json.dumps({"success": False, "error": f"执行时发生错误: {str(e)}"}, ensure_ascii=False))

if __name__ == "__main__":
    main()
