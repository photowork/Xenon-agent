#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Debug 调试工具 (改进版)
"""

import json, sys, os, time, traceback, inspect, gc, platform
import bdb, linecache, ast, threading, queue, io
from typing import Dict, Any, Optional, List, Callable
from datetime import datetime
from pathlib import Path
from enum import Enum
from dataclasses import dataclass, field
from collections import defaultdict


class DebugError(Exception): pass
class BreakpointError(DebugError): pass
class StateError(DebugError): pass
class SecurityError(DebugError): pass
class ResourceLimitError(DebugError): pass

MAX_BREAKPOINTS = 1000
MAX_WATCH_HISTORY = 100
MAX_VARIABLE_REPR_LENGTH = 1000
MAX_CALL_STACK_DEPTH = 100

SAFE_BUILTINS = {
    'abs': abs, 'all': all, 'any': any, 'bin': bin, 'bool': bool,
    'chr': chr, 'dict': dict, 'enumerate': enumerate, 'filter': filter,
    'float': float, 'format': format, 'frozenset': frozenset, 'hex': hex,
    'int': int, 'isinstance': isinstance, 'iter': iter, 'len': len,
    'list': list, 'map': map, 'max': max, 'min': min, 'next': next,
    'oct': oct, 'ord': ord, 'pow': pow, 'print': print, 'range': range,
    'repr': repr, 'reversed': reversed, 'round': round, 'set': set,
    'slice': slice, 'sorted': sorted, 'str': str, 'sum': sum,
    'tuple': tuple, 'type': type, 'zip': zip,
    'True': True, 'False': False, 'None': None,
}


def safe_eval(expr: str, globals_dict: Dict = None, locals_dict: Dict = None) -> Any:
    try:
        return ast.literal_eval(expr)
    except (ValueError, SyntaxError):
        pass
    safe_globals = {"__builtins__": SAFE_BUILTINS}
    if globals_dict:
        for k in globals_dict:
            if not k.startswith('_'):
                safe_globals[k] = globals_dict[k]
    try:
        return eval(expr, safe_globals, locals_dict or {})
    except Exception as e:
        raise SecurityError(f"表达式求值失败: {e}")


def safe_exec(code_str: str, globals_dict: Dict = None, locals_dict: Dict = None) -> None:
    safe_globals = {"__builtins__": SAFE_BUILTINS}
    if globals_dict:
        for k in globals_dict:
            if not k.startswith('_'):
                safe_globals[k] = globals_dict[k]
    try:
        exec(code_str, safe_globals, locals_dict or {})
    except Exception as e:
        raise SecurityError(f"代码执行失败: {e}")


try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

try:
    import cProfile, pstats
    HAS_PROFILE = True
except ImportError:
    HAS_PROFILE = False


class DebugState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STEPPING = "stepping"
    STEP_OVER = "step_over"
    STEP_OUT = "step_out"
    TERMINATED = "terminated"


@dataclass
class Breakpoint:
    id: int
    file_path: str
    line_number: int
    condition: Optional[str] = None
    enabled: bool = True
    hit_count: int = 0
    ignore_count: int = 0
    temporary: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "file_path": self.file_path,
            "line_number": self.line_number, "condition": self.condition,
            "enabled": self.enabled, "hit_count": self.hit_count,
            "ignore_count": self.ignore_count, "temporary": self.temporary
        }


@dataclass
class StackFrame:
    index: int
    file_path: str
    line_number: int
    function_name: str
    code_line: Optional[str] = None
    locals: Dict[str, Any] = field(default_factory=dict)
    globals: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "file_path": self.file_path,
            "line_number": self.line_number, "function_name": self.function_name,
            "code_line": self.code_line,
            "locals": {k: repr(v)[:200] for k, v in self.locals.items()},
            "globals": {k: repr(v)[:200] for k, v in list(self.globals.items())[:20]}
        }


class DebuggerCore(bdb.Bdb):
    def __init__(self):
        super().__init__()
        self._state_lock = threading.RLock()
        self._state = DebugState.IDLE
        self.breakpoints: Dict[int, Breakpoint] = {}
        self._breakpoints_by_file: Dict[str, List[Breakpoint]] = defaultdict(list)
        self._breakpoint_id_counter = 0
        self._current_frame = None
        self._call_stack: List[StackFrame] = []
        self._step_target_frame = None
        self._step_depth = 0
        self._control_queue: Optional[queue.Queue] = None
        self._on_breakpoint_hit: Optional[Callable] = None
        self._on_step_complete: Optional[Callable] = None
        self._on_exception: Optional[Callable] = None
        self._on_execution_complete: Optional[Callable] = None
        self._exec_globals: Dict[str, Any] = {}
        self._exec_locals: Dict[str, Any] = {}
        self._debug_thread: Optional[threading.Thread] = None
        self._last_exception: Optional[tuple] = None

    @property
    def state(self) -> DebugState:
        with self._state_lock:
            return self._state

    @state.setter
    def state(self, value: DebugState):
        with self._state_lock:
            self._state = value

    def set_breakpoint(self, file_path: str, line_number: int,
                       condition: str = None, temporary: bool = False) -> Dict[str, Any]:
        try:
            abs_path = str(Path(file_path).resolve())
            if not os.path.exists(abs_path):
                raise BreakpointError(f"文件不存在: {abs_path}")
            with self._state_lock:
                if len(self.breakpoints) >= MAX_BREAKPOINTS:
                    raise ResourceLimitError(f"断点数量已达上限 ({MAX_BREAKPOINTS})")
                for bp in self._breakpoints_by_file.get(abs_path, []):
                    if bp.line_number == line_number:
                        raise BreakpointError(f"断点已存在 (ID: {bp.id})")
                self._breakpoint_id_counter += 1
                bp_id = self._breakpoint_id_counter
                bp = Breakpoint(id=bp_id, file_path=abs_path, line_number=line_number,
                               condition=condition, temporary=temporary)
                self.breakpoints[bp_id] = bp
                self._breakpoints_by_file[abs_path].append(bp)
            try:
                self.set_break(abs_path, line_number)
            except Exception:
                pass
            return {"success": True, "breakpoint": bp.to_dict(),
                    "message": f"断点 #{bp_id} 已设置: {abs_path}:{line_number}"}
        except DebugError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": f"设置断点失败: {e}"}

    def remove_breakpoint(self, bp_id: int) -> Dict[str, Any]:
        try:
            with self._state_lock:
                if bp_id not in self.breakpoints:
                    raise BreakpointError(f"断点 #{bp_id} 不存在")
                bp = self.breakpoints.pop(bp_id)
                self._breakpoints_by_file[bp.file_path] = [
                    b for b in self._breakpoints_by_file[bp.file_path] if b.id != bp_id
                ]
                if not self._breakpoints_by_file[bp.file_path]:
                    del self._breakpoints_by_file[bp.file_path]
            try:
                self.clear_break(bp.file_path, bp.line_number)
            except Exception:
                pass
            return {"success": True, "message": f"断点 #{bp_id} 已移除"}
        except DebugError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": f"移除断点失败: {e}"}

    def remove_all_breakpoints(self) -> Dict[str, Any]:
        try:
            with self._state_lock:
                count = len(self.breakpoints)
                for bp in list(self.breakpoints.values()):
                    try:
                        self.clear_break(bp.file_path, bp.line_number)
                    except Exception:
                        pass
                self.breakpoints.clear()
                self._breakpoints_by_file.clear()
            return {"success": True, "message": f"已移除 {count} 个断点"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def list_breakpoints(self) -> Dict[str, Any]:
        with self._state_lock:
            return {"success": True, "count": len(self.breakpoints),
                    "max_limit": MAX_BREAKPOINTS,
                    "breakpoints": [bp.to_dict() for bp in self.breakpoints.values()]}

    def enable_breakpoint(self, bp_id: int, enabled: bool = True) -> Dict[str, Any]:
        try:
            with self._state_lock:
                if bp_id not in self.breakpoints:
                    raise BreakpointError(f"断点 #{bp_id} 不存在")
                self.breakpoints[bp_id].enabled = enabled
            return {"success": True, "message": f"断点 #{bp_id} 已{'启用' if enabled else '禁用'}"}
        except DebugError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def set_condition(self, bp_id: int, condition: str) -> Dict[str, Any]:
        try:
            with self._state_lock:
                if bp_id not in self.breakpoints:
                    raise BreakpointError(f"断点 #{bp_id} 不存在")
                self.breakpoints[bp_id].condition = condition
            return {"success": True, "message": f"断点 #{bp_id} 条件已设置: {condition}"}
        except DebugError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def user_line(self, frame):
        if self.state == DebugState.IDLE:
            return
        self._current_frame = frame
        self._update_call_stack(frame)
        filename = frame.f_code.co_filename
        lineno = frame.f_lineno

        with self._state_lock:
            for bp in self._breakpoints_by_file.get(filename, []):
                if bp.line_number != lineno or not bp.enabled:
                    continue
                bp.hit_count += 1
                if bp.ignore_count > 0:
                    bp.ignore_count -= 1
                    continue
                if bp.condition:
                    try:
                        if not safe_eval(bp.condition, frame.f_globals, frame.f_locals):
                            continue
                    except Exception:
                        continue
                if bp.temporary:
                    self.breakpoints.pop(bp.id, None)
                    self._breakpoints_by_file[filename] = [
                        b for b in self._breakpoints_by_file[filename] if b.id != bp.id
                    ]
                    try:
                        self.clear_break(filename, lineno)
                    except Exception:
                        pass
                self._handle_breakpoint_hit(bp, frame)
                return

        current_state = self.state
        if current_state == DebugState.STEPPING:
            self._handle_step_complete(frame)
        elif current_state == DebugState.STEP_OVER:
            if frame is self._step_target_frame:
                self._handle_step_complete(frame)
        elif current_state == DebugState.STEP_OUT:
            if len(self._call_stack) <= self._step_depth:
                self._handle_step_complete(frame)

    def user_call(self, frame, argument_list): pass
    def user_return(self, frame, return_value): pass

    def user_exception(self, frame, exc_info):
        self._last_exception = exc_info
        if self._on_exception:
            try:
                self._on_exception(frame, exc_info)
            except Exception:
                pass

    def _handle_breakpoint_hit(self, bp, frame):
        self.state = DebugState.PAUSED
        if self._on_breakpoint_hit:
            try:
                self._on_breakpoint_hit(bp, frame)
            except Exception:
                pass
        self._wait_for_control()

    def _handle_step_complete(self, frame):
        self.state = DebugState.PAUSED
        if self._on_step_complete:
            try:
                self._on_step_complete(frame)
            except Exception:
                pass
        self._wait_for_control()

    def _wait_for_control(self):
        if self._control_queue is None:
            return
        while True:
            try:
                action = self._control_queue.get(timeout=0.1)
                if action == "continue":
                    self.state = DebugState.RUNNING
                    break
                elif action == "step_into":
                    self.state = DebugState.STEPPING
                    break
                elif action == "step_over":
                    self.state = DebugState.STEP_OVER
                    self._step_target_frame = self._current_frame
                    break
                elif action == "step_out":
                    self.state = DebugState.STEP_OUT
                    self._step_depth = len(self._call_stack)
                    break
                elif action == "stop":
                    self.state = DebugState.TERMINATED
                    self.set_quit()
                    break
            except queue.Empty:
                if self.state == DebugState.TERMINATED:
                    self.set_quit()
                    break

    def _update_call_stack(self, frame):
        self._call_stack = []
        idx, current = 0, frame
        while current is not None and idx < MAX_CALL_STACK_DEPTH:
            code_line = None
            try:
                code_line = linecache.getline(current.f_code.co_filename, current.f_lineno).strip()
            except Exception:
                pass
            self._call_stack.append(StackFrame(
                index=idx, file_path=current.f_code.co_filename,
                line_number=current.f_lineno, function_name=current.f_code.co_name,
                code_line=code_line, locals=dict(current.f_locals),
                globals=dict(current.f_globals)
            ))
            current = current.f_back
            idx += 1

    def continue_execution(self) -> Dict[str, Any]:
        try:
            with self._state_lock:
                if self.state != DebugState.PAUSED:
                    raise StateError(f"当前状态为 {self.state.value}，无法继续执行")
            if self._control_queue:
                self._control_queue.put("continue")
            return {"success": True, "message": "继续执行"}
        except DebugError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def step_into(self) -> Dict[str, Any]:
        try:
            with self._state_lock:
                if self.state != DebugState.PAUSED:
                    raise StateError(f"当前状态为 {self.state.value}，无法单步执行")
            if self._control_queue:
                self._control_queue.put("step_into")
            return {"success": True, "message": "单步进入"}
        except DebugError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def step_over(self) -> Dict[str, Any]:
        try:
            with self._state_lock:
                if self.state != DebugState.PAUSED:
                    raise StateError(f"当前状态为 {self.state.value}，无法单步执行")
            if self._control_queue:
                self._control_queue.put("step_over")
            return {"success": True, "message": "单步跳过"}
        except DebugError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def step_out(self) -> Dict[str, Any]:
        try:
            with self._state_lock:
                if self.state != DebugState.PAUSED:
                    raise StateError(f"当前状态为 {self.state.value}，无法单步执行")
            if self._control_queue:
                self._control_queue.put("step_out")
            return {"success": True, "message": "单步跳出"}
        except DebugError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def stop_execution(self) -> Dict[str, Any]:
        try:
            with self._state_lock:
                self.state = DebugState.TERMINATED
            if self._control_queue:
                self._control_queue.put("stop")
            return {"success": True, "message": "执行已停止"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_call_stack(self) -> Dict[str, Any]:
        with self._state_lock:
            return {"success": True, "stack_depth": len(self._call_stack),
                    "call_stack": [f.to_dict() for f in self._call_stack]}

    def get_current_frame(self) -> Dict[str, Any]:
        if self._current_frame is None:
            return {"success": False, "error": "没有活动的调试帧"}
        frame = self._current_frame
        code_line = None
        try:
            code_line = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
        except Exception:
            pass
        return {
            "success": True,
            "current_frame": {
                "file_path": frame.f_code.co_filename,
                "line_number": frame.f_lineno,
                "function_name": frame.f_code.co_name,
                "code_line": code_line,
                "locals": {k: repr(v)[:500] for k, v in frame.f_locals.items()},
                "globals": {k: repr(v)[:500] for k, v in list(frame.f_globals.items())[:30]}
            }
        }

    def get_variable(self, var_name: str, frame_index: int = 0) -> Dict[str, Any]:
        try:
            if frame_index >= len(self._call_stack):
                raise StateError(f"帧索引 {frame_index} 超出范围")
            frame = self._call_stack[frame_index]
            if var_name in frame.locals:
                value, source = frame.locals[var_name], "local"
            elif var_name in frame.globals:
                value, source = frame.globals[var_name], "global"
            else:
                return {"success": False, "error": f"变量 '{var_name}' 未找到",
                        "available_locals": list(frame.locals.keys())[:20]}
            return {"success": True, "variable": {
                "name": var_name, "value": repr(value)[:MAX_VARIABLE_REPR_LENGTH],
                "type": type(value).__name__, "source": source
            }}
        except DebugError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def set_variable(self, var_name: str, var_value_str: str, frame_index: int = 0) -> Dict[str, Any]:
        """修复：正确使用 frame_index 参数"""
        try:
            if frame_index >= len(self._call_stack):
                raise StateError(f"帧索引 {frame_index} 超出范围")
            frame = self._current_frame
            for _ in range(frame_index):
                if frame is None:
                    break
                frame = frame.f_back
            if frame is None:
                raise StateError("无法获取目标栈帧")
            try:
                var_value = safe_eval(var_value_str, frame.f_globals, frame.f_locals)
            except SecurityError:
                var_value = var_value_str
            if var_name in frame.f_locals:
                frame.f_locals[var_name] = var_value
                source = "local"
            else:
                frame.f_globals[var_name] = var_value
                source = "global"
            self._update_call_stack(self._current_frame)
            return {"success": True,
                    "message": f"变量 '{var_name}' 已设置为 {repr(var_value)[:100]}",
                    "variable": {"name": var_name, "value": repr(var_value)[:MAX_VARIABLE_REPR_LENGTH],
                                "type": type(var_value).__name__, "source": source,
                                "frame_index": frame_index}}
        except DebugError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def execute_code(self, code_str: str, frame_index: int = 0) -> Dict[str, Any]:
        old_stdout = None
        try:
            if frame_index >= len(self._call_stack):
                raise StateError(f"帧索引 {frame_index} 超出范围")
            frame_info = self._call_stack[frame_index]
            old_stdout = sys.stdout
            captured_output = io.StringIO()
            sys.stdout = captured_output
            try:
                result = safe_eval(code_str, frame_info.globals, frame_info.locals)
                return {"success": True, "result": repr(result)[:MAX_VARIABLE_REPR_LENGTH],
                        "output": captured_output.getvalue(), "result_type": type(result).__name__}
            except SecurityError:
                safe_exec(code_str, frame_info.globals, frame_info.locals)
                return {"success": True, "output": captured_output.getvalue(), "message": "代码执行完成"}
            finally:
                if old_stdout is not None:
                    sys.stdout = old_stdout
        except DebugError as e:
            if old_stdout is not None:
                sys.stdout = old_stdout
            return {"success": False, "error": str(e)}
        except Exception as e:
            if old_stdout is not None:
                sys.stdout = old_stdout
            return {"success": False, "error": str(e), "traceback": traceback.format_exc()}

    def run_code(self, code_str: str, globals_dict: Dict = None, locals_dict: Dict = None) -> Dict[str, Any]:
        try:
            with self._state_lock:
                self.state = DebugState.RUNNING
                self._exec_globals = globals_dict or {'__name__': '__debug__', '__builtins__': SAFE_BUILTINS}
                self._exec_locals = locals_dict or {}
                self._last_exception = None
            try:
                code_obj = compile(code_str, '<debug>', 'exec')
            except SyntaxError as e:
                return {"success": False, "error": f"语法错误: {e.msg}", "line": e.lineno}
            self.reset()
            self._control_queue = queue.Queue()

            def run_target():
                try:
                    self.runcall(exec, code_obj, self._exec_globals, self._exec_locals)
                except bdb.BdbQuit:
                    pass
                except Exception:
                    self._last_exception = sys.exc_info()
                    if self._on_exception:
                        try:
                            self._on_exception(None, sys.exc_info())
                        except Exception:
                            pass
                finally:
                    self.state = DebugState.TERMINATED
                    if self._on_execution_complete:
                        try:
                            self._on_execution_complete(self._last_exception)
                        except Exception:
                            pass

            self._debug_thread = threading.Thread(target=run_target, daemon=True, name="debug-target")
            self._debug_thread.start()
            return {"success": True, "message": "代码调试已启动", "state": self.state.value}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def run_file(self, file_path: str, args: List[str] = None) -> Dict[str, Any]:
        try:
            abs_path = str(Path(file_path).resolve())
            if not os.path.exists(abs_path):
                raise DebugError(f"文件不存在: {abs_path}")
            with open(abs_path, 'r', encoding='utf-8') as f:
                code_str = f.read()
            globals_dict = {'__name__': '__main__', '__file__': abs_path,
                           '__builtins__': SAFE_BUILTINS, 'sys': sys, 'os': os}
            if args:
                sys.argv = [abs_path] + args
            return self.run_code(code_str, globals_dict)
        except DebugError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_state(self) -> Dict[str, Any]:
        with self._state_lock:
            return {"success": True, "state": self._state.value,
                    "breakpoints_count": len(self.breakpoints),
                    "call_stack_depth": len(self._call_stack),
                    "current_file": self._current_frame.f_code.co_filename if self._current_frame else None,
                    "current_line": self._current_frame.f_lineno if self._current_frame else None,
                    "thread_alive": self._debug_thread.is_alive() if self._debug_thread else False}

    def wait_for_pause(self, timeout: float = None) -> bool:
        start_time = time.time()
        while self.state not in (DebugState.PAUSED, DebugState.TERMINATED):
            if timeout and (time.time() - start_time) > timeout:
                return False
            time.sleep(0.01)
        return self.state == DebugState.PAUSED
class InteractiveDebugger:
    """交互式调试器"""
    def __init__(self):
        self.core = DebuggerCore()
        self._command_history: List[str] = []
        self._output_callback: Optional[Callable] = None
        self.core._on_breakpoint_hit = self._on_breakpoint_hit
        self.core._on_step_complete = self._on_step_complete
        self.core._on_exception = self._on_exception
        self.core._on_execution_complete = self._on_execution_complete

    def _on_breakpoint_hit(self, bp, frame):
        if self._output_callback:
            self._output_callback({
                "event": "breakpoint_hit", "breakpoint": bp.to_dict(),
                "location": {"file": bp.file_path, "line": bp.line_number}
            })

    def _on_step_complete(self, frame):
        if self._output_callback:
            self._output_callback({
                "event": "step_complete",
                "location": {"file": frame.f_code.co_filename, "line": frame.f_lineno,
                            "function": frame.f_code.co_name}
            })

    def _on_exception(self, frame, exc_info):
        if self._output_callback:
            self._output_callback({
                "event": "exception",
                "exception": {"type": exc_info[0].__name__ if exc_info[0] else None,
                             "message": str(exc_info[1])}
            })

    def _on_execution_complete(self, last_exception):
        if self._output_callback:
            self._output_callback({
                "event": "execution_complete",
                "has_exception": last_exception is not None
            })

    def set_output_callback(self, callback: Callable):
        self._output_callback = callback

    def process_command(self, command: str) -> Dict[str, Any]:
        if not command or not command.strip():
            return {"success": False, "error": "空命令"}
        self._command_history.append(command)
        parts = command.strip().split(maxsplit=1)
        cmd, args = parts[0].lower(), parts[1] if len(parts) > 1 else ""

        handlers = {
            'b': self._cmd_break, 'break': self._cmd_break,
            'clear': self._cmd_clear, 'disable': self._cmd_disable,
            'enable': self._cmd_enable, 'condition': self._cmd_condition,
            'info': self._cmd_info, 'list': self._cmd_list,
            'where': self._cmd_where, 'bt': self._cmd_where,
            'p': self._cmd_print, 'print': self._cmd_print,
            'set': self._cmd_set, 'n': self._cmd_next, 'next': self._cmd_next,
            's': self._cmd_step, 'step': self._cmd_step,
            'finish': self._cmd_finish, 'c': self._cmd_continue,
            'continue': self._cmd_continue, 'quit': self._cmd_quit, 'q': self._cmd_quit,
            'help': self._cmd_help, 'h': self._cmd_help, '!': self._cmd_exec,
        }
        handler = handlers.get(cmd)
        return handler(args) if handler else self._cmd_print(command)

    def _cmd_break(self, args):
        if not args:
            return self.core.list_breakpoints()
        condition = None
        if ' if ' in args:
            args, condition = args.split(' if ', 1)
            condition = condition.strip()
        try:
            if ':' in args:
                file_path, line_str = args.rsplit(':', 1)
                line_number = int(line_str.strip())
            else:
                line_number = int(args.strip())
                if self.core._current_frame:
                    file_path = self.core._current_frame.f_code.co_filename
                else:
                    return {"success": False, "error": "请指定文件路径"}
            return self.core.set_breakpoint(file_path, line_number, condition)
        except ValueError:
            return {"success": False, "error": "无效的行号"}

    def _cmd_clear(self, args):
        if not args:
            return self.core.remove_all_breakpoints()
        try:
            return self.core.remove_breakpoint(int(args.strip()))
        except ValueError:
            return {"success": False, "error": "无效的断点ID"}

    def _cmd_disable(self, args):
        try:
            return self.core.enable_breakpoint(int(args.strip()), False)
        except ValueError:
            return {"success": False, "error": "无效的断点ID"}

    def _cmd_enable(self, args):
        try:
            return self.core.enable_breakpoint(int(args.strip()), True)
        except ValueError:
            return {"success": False, "error": "无效的断点ID"}

    def _cmd_condition(self, args):
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return {"success": False, "error": "用法: condition bp_id expression"}
        try:
            return self.core.set_condition(int(parts[0]), parts[1])
        except ValueError:
            return {"success": False, "error": "无效的断点ID"}

    def _cmd_info(self, args):
        subcmd = (args.strip().lower() if args else "breakpoints")
        if subcmd in ('breakpoints', 'break', 'b'):
            return self.core.list_breakpoints()
        elif subcmd in ('locals', 'local', 'lo'):
            fi = self.core.get_current_frame()
            return {"success": True, "locals": fi["current_frame"]["locals"]} if fi.get("success") else fi
        elif subcmd in ('globals', 'global', 'gl'):
            fi = self.core.get_current_frame()
            return {"success": True, "globals": fi["current_frame"]["globals"]} if fi.get("success") else fi
        elif subcmd in ('source', 'src'):
            return self.core.get_current_frame()
        elif subcmd in ('stack', 'st'):
            return self.core.get_call_stack()
        return {"success": False, "error": f"未知信息类型: {subcmd}"}

    def _cmd_list(self, args):
        fi = self.core.get_current_frame()
        return fi if not fi.get("success") else {"success": True, "source": fi["current_frame"]}

    def _cmd_where(self, args):
        return self.core.get_call_stack()

    def _cmd_print(self, args):
        return {"success": False, "error": "请指定表达式"} if not args else self.core.get_variable(args.strip())

    def _cmd_set(self, args):
        if '=' not in args:
            return {"success": False, "error": "用法: set var_name = value"}
        var_name, value_str = args.split('=', 1)
        return self.core.set_variable(var_name.strip(), value_str.strip())

    def _cmd_next(self, args): return self.core.step_over()
    def _cmd_step(self, args): return self.core.step_into()
    def _cmd_finish(self, args): return self.core.step_out()
    def _cmd_continue(self, args): return self.core.continue_execution()
    def _cmd_quit(self, args):
        self.core.stop_execution()
        return {"success": True, "message": "调试会话已结束"}
    def _cmd_exec(self, args): return self.core.execute_code(args)

    def _cmd_help(self, args):
        return {"success": True, "help": {
            "断点": {"b line [if cond]": "设置断点", "clear [id]": "清除断点",
                    "disable/enable id": "禁用/启用", "condition id expr": "条件断点"},
            "执行": {"n/next": "单步跳过", "s/step": "单步进入", "finish": "跳出函数",
                    "c/continue": "继续", "q/quit": "停止"},
            "查看": {"p expr": "打印变量", "set var=val": "设置变量",
                    "info [breakpoints|locals|globals|stack]": "查看信息",
                    "where/bt": "调用栈", "list": "源代码"},
            "其他": {"h/help": "帮助", "!stmt": "执行语句"}
        }}


class DebugHandler:
    """调试工具处理器 - 提供断点调试、性能分析、变量监视等功能"""
    def __init__(self, log_dir: str = None):
        self.log_dir = Path(log_dir) if log_dir else Path.cwd() / "debug_logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._timers: Dict[str, float] = {}
        self._counters: Dict[str, int] = {}
        self._watch_vars: Dict[str, List[Dict[str, Any]]] = {}
        self._debugger = InteractiveDebugger()

    @property
    def debugger(self) -> InteractiveDebugger:
        return self._debugger

    def set_breakpoint(self, file_path: str, line_number: int, condition: str = None, temporary: bool = False) -> Dict[str, Any]:
        """
        在指定文件的某一行设置断点
        :param file_path: 目标文件路径
        :param line_number: 断点所在行号
        :param condition: 可选的条件表达式，当条件为真时才触发断点
        :param temporary: 是否为临时断点（触发一次后自动删除）
        :return: 包含断点ID和状态的字典
        """
        return self._debugger.core.set_breakpoint(file_path, line_number, condition, temporary)

    def remove_breakpoint(self, bp_id: int) -> Dict[str, Any]:
        """
        移除指定ID的断点
        :param bp_id: 断点ID
        :return: 操作结果字典
        """
        return self._debugger.core.remove_breakpoint(bp_id)

    def remove_all_breakpoints(self) -> Dict[str, Any]:
        """
        移除所有断点
        :return: 操作结果字典，包含移除的断点数量
        """
        return self._debugger.core.remove_all_breakpoints()

    def list_breakpoints(self) -> Dict[str, Any]:
        """
        列出所有已设置的断点
        :return: 包含所有断点信息的字典
        """
        return self._debugger.core.list_breakpoints()

    def enable_breakpoint(self, bp_id: int, enabled: bool = True) -> Dict[str, Any]:
        """
        启用或禁用指定断点
        :param bp_id: 断点ID
        :param enabled: True启用，False禁用
        :return: 操作结果字典
        """
        return self._debugger.core.enable_breakpoint(bp_id, enabled)

    def set_condition(self, bp_id: int, condition: str) -> Dict[str, Any]:
        """
        为断点设置条件表达式
        :param bp_id: 断点ID
        :param condition: 条件表达式字符串
        :return: 操作结果字典
        """
        return self._debugger.core.set_condition(bp_id, condition)

    def run_code(self, code: str, globals_dict: Dict = None, locals_dict: Dict = None) -> Dict[str, Any]:
        """
        在调试模式下执行代码字符串
        :param code: 要执行的Python代码
        :param globals_dict: 全局命名空间字典
        :param locals_dict: 局部命名空间字典
        :return: 执行结果字典
        """
        return self._debugger.core.run_code(code, globals_dict, locals_dict)

    def run_file(self, file_path: str, args: List[str] = None) -> Dict[str, Any]:
        """
        在调试模式下运行Python文件
        :param file_path: Python文件路径
        :param args: 命令行参数列表
        :return: 执行结果字典
        """
        return self._debugger.core.run_file(file_path, args)

    def continue_execution(self) -> Dict[str, Any]:
        """
        继续执行程序直到下一个断点或程序结束
        :return: 操作结果字典
        """
        return self._debugger.core.continue_execution()

    def step_into(self) -> Dict[str, Any]:
        """
        单步进入：执行下一行代码，如果是函数调用则进入函数内部
        :return: 操作结果字典
        """
        return self._debugger.core.step_into()

    def step_over(self) -> Dict[str, Any]:
        """
        单步跳过：执行下一行代码，如果是函数调用则跳过整个函数
        :return: 操作结果字典
        """
        return self._debugger.core.step_over()

    def step_out(self) -> Dict[str, Any]:
        """
        单步跳出：执行当前函数剩余代码并返回到调用者
        :return: 操作结果字典
        """
        return self._debugger.core.step_out()

    def stop_execution(self) -> Dict[str, Any]:
        """
        停止当前调试会话
        :return: 操作结果字典
        """
        return self._debugger.core.stop_execution()

    def get_call_stack(self) -> Dict[str, Any]:
        """
        获取当前调用栈信息
        :return: 包含调用栈各层信息的字典
        """
        return self._debugger.core.get_call_stack()

    def get_current_frame(self) -> Dict[str, Any]:
        """
        获取当前执行帧的信息（文件、行号、函数名、局部变量等）
        :return: 当前帧详细信息字典
        """
        return self._debugger.core.get_current_frame()

    def get_variable(self, var_name: str, frame_index: int = 0) -> Dict[str, Any]:
        """
        获取指定变量的值
        :param var_name: 变量名
        :param frame_index: 栈帧索引，0为当前帧
        :return: 变量值和类型信息字典
        """
        return self._debugger.core.get_variable(var_name, frame_index)

    def set_variable(self, var_name: str, var_value_str: str, frame_index: int = 0) -> Dict[str, Any]:
        """
        设置指定变量的值
        :param var_name: 变量名
        :param var_value_str: 变量值的字符串表示
        :param frame_index: 栈帧索引，0为当前帧
        :return: 操作结果字典
        """
        return self._debugger.core.set_variable(var_name, var_value_str, frame_index)

    def execute_code(self, code: str, frame_index: int = 0) -> Dict[str, Any]:
        """
        在指定栈帧的上下文中执行代码
        :param code: 要执行的Python代码
        :param frame_index: 栈帧索引
        :return: 执行结果字典
        """
        return self._debugger.core.execute_code(code, frame_index)

    def process_command(self, command: str) -> Dict[str, Any]:
        """
        处理交互式调试命令（如 'b file.py:10', 'n', 'c', 'p var' 等）
        :param command: 调试命令字符串
        :return: 命令执行结果字典
        """
        return self._debugger.process_command(command)

    def get_debugger_state(self) -> Dict[str, Any]:
        """
        获取调试器当前状态（idle/running/paused/stepping/terminated）
        :return: 状态信息字典
        """
        return self._debugger.core.get_state()

    def wait_for_pause(self, timeout: float = None) -> Dict[str, Any]:
        """
        等待调试器进入暂停状态
        :param timeout: 超时时间（秒）
        :return: 状态信息字典
        """
        return self._debugger.core.wait_for_pause(timeout)

    def start_timer(self, timer_name: str) -> Dict[str, Any]:
        """
        启动一个命名计时器
        :param timer_name: 计时器名称
        :return: 操作结果字典
        """
        with self._lock:
            self._timers[timer_name] = time.perf_counter()
        return {"success": True, "message": f"计时器 '{timer_name}' 已启动"}

    def stop_timer(self, timer_name: str) -> Dict[str, Any]:
        """
        停止计时器并返回耗时
        :param timer_name: 计时器名称
        :return: 包含耗时信息的字典（秒和毫秒）
        """
        with self._lock:
            if timer_name not in self._timers:
                return {"success": False, "error": f"计时器 '{timer_name}' 不存在"}
            elapsed = time.perf_counter() - self._timers.pop(timer_name)
        return {"success": True, "elapsed_seconds": round(elapsed, 6),
                "elapsed_ms": round(elapsed * 1000, 3),
                "message": f"耗时: {elapsed:.6f} 秒"}

    def list_timers(self) -> Dict[str, Any]:
        """
        列出所有活动计时器
        :return: 计时器名称列表
        """
        with self._lock:
            return {"success": True, "active_timers": list(self._timers.keys()), "count": len(self._timers)}

    def increment_counter(self, counter_name: str, amount: int = 1) -> Dict[str, Any]:
        """
        递增计数器
        :param counter_name: 计数器名称
        :param amount: 递增量，默认为1
        :return: 包含当前计数值的字典
        """
        with self._lock:
            self._counters[counter_name] = self._counters.get(counter_name, 0) + amount
            val = self._counters[counter_name]
        return {"success": True, "counter_name": counter_name, "current_value": val}

    def get_counter(self, counter_name: str) -> Dict[str, Any]:
        """
        获取计数器当前值
        :param counter_name: 计数器名称
        :return: 计数器值字典
        """
        with self._lock:
            if counter_name not in self._counters:
                return {"success": False, "error": f"计数器 '{counter_name}' 不存在"}
            return {"success": True, "counter_name": counter_name, "value": self._counters[counter_name]}

    def list_counters(self) -> Dict[str, Any]:
        """
        列出所有计数器及其值
        :return: 所有计数器字典
        """
        with self._lock:
            return {"success": True, "counters": dict(self._counters), "count": len(self._counters)}

    def reset_counter(self, counter_name: str = None) -> Dict[str, Any]:
        """
        重置计数器
        :param counter_name: 计数器名称，为None时重置所有计数器
        :return: 操作结果字典
        """
        with self._lock:
            if counter_name:
                if counter_name in self._counters:
                    del self._counters[counter_name]
                    return {"success": True, "message": f"计数器 '{counter_name}' 已重置"}
                return {"success": False, "error": f"计数器 '{counter_name}' 不存在"}
            self._counters.clear()
            return {"success": True, "message": "所有计数器已重置"}

    def watch_variable(self, var_name: str, var_value: Any = None) -> Dict[str, Any]:
        """
        监视变量值的变化（记录历史）
        :param var_name: 变量名
        :param var_value: 变量值（可选，不提供时自动从调用者上下文获取）
        :return: 监视记录信息字典
        """
        ts = datetime.now().isoformat()
        if var_value is None:
            frame = inspect.currentframe()
            try:
                caller_locals = frame.f_back.f_locals
                if var_name not in caller_locals:
                    return {"success": False, "error": f"变量 '{var_name}' 未找到"}
                var_value = caller_locals[var_name]
            finally:
                del frame
        with self._lock:
            if var_name not in self._watch_vars:
                self._watch_vars[var_name] = []
            if len(self._watch_vars[var_name]) >= MAX_WATCH_HISTORY:
                self._watch_vars[var_name].pop(0)
            self._watch_vars[var_name].append({
                "timestamp": ts, "value": repr(var_value)[:MAX_VARIABLE_REPR_LENGTH],
                "type": type(var_value).__name__
            })
        return {"success": True, "var_name": var_name, "watch_count": len(self._watch_vars[var_name])}

    def get_watch_history(self, var_name: str = None) -> Dict[str, Any]:
        """
        获取变量的监视历史记录
        :param var_name: 变量名，为None时返回所有被监视变量
        :return: 监视历史字典
        """
        with self._lock:
            if var_name:
                if var_name not in self._watch_vars:
                    return {"success": False, "error": f"变量 '{var_name}' 未被监视"}
                return {"success": True, "var_name": var_name, "history": list(self._watch_vars[var_name])}
            return {"success": True, "watched_variables": {k: len(v) for k, v in self._watch_vars.items()}}

    def clear_watch(self, var_name: str = None) -> Dict[str, Any]:
        """
        清除变量监视
        :param var_name: 变量名，为None时清除所有监视
        :return: 操作结果字典
        """
        with self._lock:
            if var_name:
                if var_name in self._watch_vars:
                    del self._watch_vars[var_name]
                    return {"success": True, "message": f"变量 '{var_name}' 监视已清除"}
                return {"success": False, "error": f"变量 '{var_name}' 未被监视"}
            self._watch_vars.clear()
            return {"success": True, "message": "所有变量监视已清除"}

    def get_system_status(self) -> Dict[str, Any]:
        """
        获取系统状态信息（平台、CPU、内存等）
        :return: 系统状态字典
        """
        result = {"success": True, "timestamp": datetime.now().isoformat(),
                  "system": {"platform": platform.system(), "python_version": platform.python_version(),
                            "cpu_count": os.cpu_count()}}
        if HAS_PSUTIL:
            process = psutil.Process()
            result["system"]["cpu_percent"] = psutil.cpu_percent(interval=0.1)
            result["system"]["memory"] = {
                "total_gb": round(psutil.virtual_memory().total / (1024**3), 2),
                "available_gb": round(psutil.virtual_memory().available / (1024**3), 2),
                "used_percent": psutil.virtual_memory().percent
            }
            result["process"] = {"pid": os.getpid(), "memory_mb": round(process.memory_info().rss / (1024**2), 2)}
        else:
            result["process"] = {"pid": os.getpid()}
            result["note"] = "psutil 未安装"
        return result

    def get_memory_usage(self) -> Dict[str, Any]:
        """
        获取当前进程内存使用情况
        :return: 内存使用信息字典
        """
        result = {"success": True, "gc_count": gc.get_count(), "objects_count": len(gc.get_objects())}
        if HAS_PSUTIL:
            mi = psutil.Process().memory_info()
            result["process_memory"] = {"rss_mb": round(mi.rss / (1024**2), 2), "vms_mb": round(mi.vms / (1024**2), 2)}
        return result

    def force_garbage_collection(self) -> Dict[str, Any]:
        """
        强制执行垃圾回收
        :return: 回收结果字典（回收对象数、释放对象数）
        """
        before = len(gc.get_objects())
        collected = gc.collect()
        after = len(gc.get_objects())
        return {"success": True, "objects_collected": collected, "objects_freed": before - after}

    def analyze_traceback(self, exc_info: tuple = None) -> Dict[str, Any]:
        """
        分析异常堆栈信息
        :param exc_info: 异常信息元组，为None时自动获取当前异常
        :return: 异常分析结果字典
        """
        if exc_info is None:
            exc_info = sys.exc_info()
        if exc_info[0] is None:
            return {"success": True, "has_exception": False}
        return {"success": True, "has_exception": True,
                "exception": {"type": exc_info[0].__name__, "message": str(exc_info[1]),
                             "full_traceback": traceback.format_exc()}}

    def log_debug(self, message: str, level: str = "INFO", data: Dict = None, save_to_file: bool = False) -> Dict[str, Any]:
        """
        记录调试日志
        :param message: 日志消息
        :param level: 日志级别（INFO/WARNING/ERROR/DEBUG）
        :param data: 附加数据字典
        :param save_to_file: 是否保存到日志文件
        :return: 日志条目字典
        """
        frame = inspect.currentframe()
        try:
            caller = frame.f_back
            log_entry = {"timestamp": datetime.now().isoformat(), "level": level.upper(), "message": message,
                        "caller": {"file": caller.f_code.co_filename, "line": caller.f_lineno}, "data": data}
        finally:
            del frame
        if save_to_file:
            log_file = self.log_dir / f"debug_{datetime.now().strftime('%Y%m%d')}.log"
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry, ensure_ascii=False, default=str) + '\n')
        return {"success": True, "log_entry": log_entry}

    def profile_function(self, func_code: str, globals_dict: Dict = None, locals_dict: Dict = None) -> Dict[str, Any]:
        """
        对代码进行性能分析（使用cProfile）
        :param func_code: 要分析的Python代码
        :param globals_dict: 全局命名空间字典
        :param locals_dict: 局部命名空间字典
        :return: 包含执行时间和性能统计的字典
        """
        if not HAS_PROFILE:
            return {"success": False, "error": "cProfile 不可用"}
        profiler = cProfile.Profile()
        start = time.perf_counter()
        profiler.enable()
        safe_exec(func_code, globals_dict or {}, locals_dict or {})
        profiler.disable()
        s = io.StringIO()
        stats = pstats.Stats(profiler, stream=s)
        stats.sort_stats('cumulative')
        stats.print_stats(20)
        return {"success": True, "execution_time": round(time.perf_counter() - start, 6),
                "profile_stats": s.getvalue()}

    def measure_execution_time(self, func_code: str, iterations: int = 1,
                               globals_dict: Dict = None, locals_dict: Dict = None) -> Dict[str, Any]:
        """
        测量代码执行时间（支持多次迭代）
        :param func_code: 要测量的Python代码
        :param iterations: 迭代次数
        :param globals_dict: 全局命名空间字典
        :param locals_dict: 局部命名空间字典
        :return: 包含统计信息（总时间、平均、最小、最大）的字典
        """
        times = []
        for _ in range(iterations):
            start = time.perf_counter()
            safe_exec(func_code, globals_dict or {}, locals_dict or {})
            times.append(time.perf_counter() - start)
        return {"success": True, "iterations": iterations,
                "statistics": {"total_time": round(sum(times), 6), "average_time": round(sum(times)/len(times), 6),
                              "min_time": round(min(times), 6), "max_time": round(max(times), 6)}}

    def list_threads(self) -> Dict[str, Any]:
        """
        列出所有活动线程
        :return: 线程信息列表（名称、ID、守护状态、存活状态）
        """
        threads = [{"name": t.name, "ident": t.ident, "daemon": t.daemon, "is_alive": t.is_alive()}
                   for t in threading.enumerate()]
        return {"success": True, "active_threads": threads, "count": len(threads)}

    def dump_state(self, file_path: str = None) -> Dict[str, Any]:
        """
        导出当前调试状态（计时器、计数器、监视变量、调试器状态）
        :param file_path: 保存文件路径，为None时仅返回不保存
        :return: 状态字典
        """
        with self._lock:
            state = {"timestamp": datetime.now().isoformat(), "timers": dict(self._timers),
                    "counters": dict(self._counters),
                    "watch_vars": {k: list(v) for k, v in self._watch_vars.items()},
                    "debugger_state": self.get_debugger_state()}
        if file_path:
            path = Path(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2, default=str)
        return {"success": True, "state": state}

    def reset_all(self) -> Dict[str, Any]:
        """
        重置所有调试状态（计时器、计数器、监视变量、断点）
        :return: 操作结果字典
        """
        with self._lock:
            self._timers.clear()
            self._counters.clear()
            self._watch_vars.clear()
        self._debugger.core.remove_all_breakpoints()
        return {"success": True, "message": "所有调试状态已重置"}


class DebugToolManager:
    """调试工具管理器 - 使用代理模式"""
    def __init__(self, log_dir: str = None):
        self._handler = DebugHandler(log_dir)

    @property
    def handler(self) -> DebugHandler:
        return self._handler

    @property
    def debugger(self) -> InteractiveDebugger:
        return self._handler._debugger

    def __getattr__(self, name: str):
        return getattr(self._handler, name)

    def __dir__(self):
        return list(set(dir(self.__class__) + dir(self._handler)))


def create_debug_tool_manager(log_dir: str = None) -> DebugToolManager:
    return DebugToolManager(log_dir)


if __name__ == "__main__":
    print("Debug Tool Manager - 使用 create_debug_tool_manager() 创建实例")