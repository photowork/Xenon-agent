from __future__ import annotations

import copy
import importlib.util
import inspect
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, get_type_hints

from xenon_core.tool_payload_runtime import sanitize_tool_arguments_for_execution

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    Observer = None
    FileSystemEventHandler = None


logger = logging.getLogger(__name__)
FILE_WATCHER_DEBOUNCE_DELAY = 2


class ToolManager:
    def __init__(self, tools_dir: Optional[Union[str, Path]] = None):
        self.tools: Dict[str, Any] = {}
        self.tool_schemas: List[Dict] = []
        self.module_names: List[str] = []
        self.tools_dir = Path(tools_dir) if tools_dir else Path(__file__).resolve().parent.parent / "Tools"
        self.observer = None
        self._debounce_timer = None
        self._debounce_lock = threading.Lock()
        self._rw_lock = threading.RLock()
        self.load_report: Dict[str, Any] = {}
        self._load_tools()

    def _load_tools(self):
        if not self.tools_dir.exists():
            logger.warning(f"工具目录 {self.tools_dir} 不存在")
            self.load_report = {
                "tools_dir": str(self.tools_dir),
                "module_file_count": 0,
                "module_names": [],
                "tool_schema_count": 0,
                "successes": [],
                "failures": [
                    {
                        "module_name": "",
                        "path": str(self.tools_dir),
                        "error": "tools directory not found",
                    }
                ],
            }
            return

        new_tools: Dict[str, Any] = {}
        new_schemas: List[Dict] = []
        loaded_modules = set()
        successes: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []
        module_files: List[Path] = []

        for file_path in self.tools_dir.rglob("*.py"):
            if file_path.name.startswith("_"):
                continue

            relative_path = file_path.relative_to(self.tools_dir)
            depth = len(relative_path.parts)
            if depth > 2:
                continue

            module_files.append(file_path)
            module_name = file_path.stem
            try:
                spec = importlib.util.spec_from_file_location(module_name, file_path)
                if not spec or not spec.loader:
                    raise ImportError("无法创建模块加载器")

                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                manager_classes = []
                for name, obj in inspect.getmembers(module):
                    if (
                        inspect.isclass(obj)
                        and (name.endswith("ToolManager") or name.endswith("Manager"))
                        and obj.__module__ == module_name
                    ):
                        manager_classes.append((name, obj))

                module_loaded = False
                for name, obj in manager_classes:
                    try:
                        manager_instance = obj()
                        tool_name = f"{module_name}_{name.replace('ToolManager', '').replace('Manager', '')}"
                        schema_count_before = len(new_schemas)
                        new_tools[tool_name] = manager_instance
                        self._generate_tool_schema_to_list(tool_name, manager_instance, new_schemas)
                        generated_schema_count = len(new_schemas) - schema_count_before
                        successes.append(
                            {
                                "module_name": module_name,
                                "manager_class": name,
                                "tool_name": tool_name,
                                "path": str(file_path),
                                "schema_count": generated_schema_count,
                            }
                        )
                        module_loaded = True
                        print(f"\033[38;2;111;208;104m[OK] Tool manager loaded: {module_name}.{name}\033[0m")
                    except Exception as exc:
                        failures.append(
                            {
                                "module_name": module_name,
                                "manager_class": name,
                                "path": str(file_path),
                                "error": str(exc),
                            }
                        )
                        logger.error(f"实例化工具管理器 {module_name}.{name} 失败: {exc}")

                if module_loaded:
                    loaded_modules.add(module_name)

            except Exception as exc:
                failures.append(
                    {
                        "module_name": module_name,
                        "path": str(file_path),
                        "error": str(exc),
                    }
                )
                logger.error(f"加载工具 {module_name} 失败: {exc}")

        with self._rw_lock:
            self.tools = new_tools
            self.tool_schemas = new_schemas
            self.module_names = sorted(loaded_modules)
            self.load_report = {
                "tools_dir": str(self.tools_dir),
                "module_file_count": len(module_files),
                "module_names": list(self.module_names),
                "tool_schema_count": len(self.tool_schemas),
                "successes": successes,
                "failures": failures,
            }

    def _generate_tool_schema_to_list(self, tool_name: str, manager_instance: Any, schema_list: List[Dict]):
        for method_name in dir(manager_instance):
            if method_name.startswith("_"):
                continue
            method = getattr(manager_instance, method_name)
            if callable(method):
                schema = {
                    "type": "function",
                    "function": {
                        "name": f"{tool_name}_{method_name}",
                        "description": self._get_method_description(method),
                        "parameters": self._get_method_parameters(method),
                    },
                }
                schema_list.append(schema)

    def _generate_tool_schema(self, tool_name: str, manager_instance: Any):
        for method_name in dir(manager_instance):
            if method_name.startswith("_"):
                continue
            method = getattr(manager_instance, method_name)
            if callable(method):
                schema = {
                    "type": "function",
                    "function": {
                        "name": f"{tool_name}_{method_name}",
                        "description": self._get_method_description(method),
                        "parameters": self._get_method_parameters(method),
                    },
                }
                self.tool_schemas.append(schema)

    def _get_method_description(self, method) -> str:
        return inspect.getdoc(method) or f"Execute {method.__name__}"

    def _get_method_parameters(self, method) -> Dict:
        sig = inspect.signature(method)
        parameters = {}
        required = []

        type_mapping = {
            "int": "integer",
            "float": "number",
            "double": "number",
            "bool": "boolean",
            "List": "array",
            "list": "array",
            "Dict": "object",
            "dict": "object",
            "Tuple": "array",
            "tuple": "array",
            "str": "string",
            "string": "string",
            "Path": "string",
            "datetime": "string",
            "Any": "string",
            "Optional": "string",
        }

        docstring = inspect.getdoc(method) or ""
        param_descriptions = {}
        if docstring:
            param_pattern = r":param\s+(\w+):\s*(.+?)(?=:param|:type|:return|:rtype|$)"
            for match in re.finditer(param_pattern, docstring, re.DOTALL):
                param_name = match.group(1)
                param_desc = match.group(2).strip().replace("\n", " ")
                param_descriptions[param_name] = param_desc

        type_hints = {}
        try:
            type_hints = get_type_hints(method)
        except Exception:
            pass

        for name, param in sig.parameters.items():
            if name == "self":
                continue

            param_type = "string"
            param_desc = param_descriptions.get(name, f"Parameter {name}")

            if name in type_hints:
                annotation = type_hints[name]
                annotation_str = str(annotation)

                if hasattr(annotation, "__origin__") and annotation.__origin__ is Union:
                    args = annotation.__args__
                    if len(args) == 2 and type(None) in args:
                        for arg in args:
                            if arg is not type(None):
                                annotation_str = str(arg)
                                break

                for key, value in type_mapping.items():
                    if key in annotation_str:
                        param_type = value
                        break
            elif param.annotation != inspect.Parameter.empty:
                annotation_str = str(param.annotation)
                for key, value in type_mapping.items():
                    if key in annotation_str:
                        param_type = value
                        break

            parameters[name] = {"type": param_type, "description": param_desc}

            if param.default == inspect.Parameter.empty:
                required.append(name)

        return {"type": "object", "properties": parameters, "required": required}

    def get_tool_schemas(self) -> List[Dict]:
        with self._rw_lock:
            return list(self.tool_schemas)

    def get_tool_list(self) -> List[str]:
        with self._rw_lock:
            return [schema["function"]["name"] for schema in self.tool_schemas]

    def get_module_list(self) -> List[str]:
        with self._rw_lock:
            return list(self.module_names)

    def get_load_report(self) -> Dict[str, Any]:
        with self._rw_lock:
            return copy.deepcopy(self.load_report)

    def get_tool_schema_by_name(self, tool_name: str) -> Optional[Dict]:
        with self._rw_lock:
            for schema in self.tool_schemas:
                if schema["function"]["name"] == tool_name:
                    return schema
            return None

    def execute_tool(self, tool_name: str, arguments: Dict) -> Any:
        try:
            with self._rw_lock:
                manager_name = next((key for key in self.tools.keys() if tool_name.startswith(key)), None)
                if not manager_name:
                    raise ValueError(f"工具 {tool_name} 不存在")

                manager = self.tools[manager_name]
                method_name = tool_name[len(manager_name) + 1 :]
                if not method_name or not hasattr(manager, method_name):
                    raise ValueError(f"方法 {method_name} 在工具 {manager_name} 中不存在")

            # Phase 4 防御：过滤上下文压缩残留键，防止被错误传参
            clean_args = sanitize_tool_arguments_for_execution(tool_name, arguments or {})
            result = getattr(manager, method_name)(**clean_args)
            logger.info(f"工具 {tool_name} 执行成功")
            return result

        except Exception as exc:
            logger.error(f"工具 {tool_name} 执行失败: {exc}")
            raise

    def start_file_watcher(self):
        if not WATCHDOG_AVAILABLE:
            print("\033[93m文件监控器不可用（watchdog 模块未安装）\033[0m")
            return

        class ToolFileHandler(FileSystemEventHandler):
            def __init__(self, tool_manager):
                self.tool_manager = tool_manager

            def on_modified(self, event):
                if event.src_path.endswith(".py"):
                    path = Path(event.src_path)
                    try:
                        relative_path = path.relative_to(self.tool_manager.tools_dir)
                        depth = len(relative_path.parts)
                        if depth <= 2:
                            logger.info(f"检测到工具文件变化: {event.src_path}")
                            with self.tool_manager._debounce_lock:
                                if self.tool_manager._debounce_timer:
                                    self.tool_manager._debounce_timer.cancel()
                                self.tool_manager._debounce_timer = threading.Timer(
                                    FILE_WATCHER_DEBOUNCE_DELAY,
                                    self._reload_tools,
                                )
                                self.tool_manager._debounce_timer.daemon = True
                                self.tool_manager._debounce_timer.start()
                    except ValueError:
                        pass

            def _reload_tools(self):
                logger.info("防抖延迟结束，重新加载工具...")
                self.tool_manager._load_tools()

        if self.observer is None:
            self.observer = Observer()
            self.observer.daemon = True
            self.observer.schedule(ToolFileHandler(self), str(self.tools_dir), recursive=True)
            self.observer.start()
            print(f"\033[38;2;70;125;181m文件监控器已启动，正在监控 {self.tools_dir}（递归两层）...\033[0m")

    def stop_file_watcher(self):
        with self._debounce_lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
                self._debounce_timer = None
        if self.observer:
            try:
                self.observer.stop()
                self.observer.join(timeout=3)
            except Exception as exc:
                logger.error(f"停止文件监控器时出错: {exc}")
            finally:
                self.observer = None

    # Phase 4: 沙箱上下文注入
    def inject_sandbox_context(self, sandbox_context) -> int:
        """将沙箱上下文注入到已加载的工具 handler 中。
        遍历所有工具实例，对拥有 sandbox_context 属性或 handler 属性的工具进行注入。
        返回成功注入的工具数量。"""
        injected = 0
        with self._rw_lock:
            for tool_name, tool_instance in self.tools.items():
                # 直接有 sandbox_context 属性
                if hasattr(tool_instance, "sandbox_context"):
                    tool_instance.sandbox_context = sandbox_context
                    injected += 1
                # 通过 .handler 属性间接注入 (如 TerminalToolManager -> TerminalHandler)
                if hasattr(tool_instance, "handler") and hasattr(tool_instance.handler, "sandbox_context"):
                    tool_instance.handler.sandbox_context = sandbox_context
                    tool_instance.handler.current_directory = str(sandbox_context.sandbox_dir)
                    injected += 1
                # 通过 .base_path 属性 (如 FileManager)
                if hasattr(tool_instance, "base_path") and sandbox_context.isolation_enabled:
                    tool_instance.base_path = sandbox_context.sandbox_dir
                    injected += 1
        return injected

    def inject_host_agent(self, agent: Any) -> int:
        """Inject the running agent into tools that explicitly support it."""
        injected = 0
        with self._rw_lock:
            for tool_instance in self.tools.values():
                attach_agent = getattr(tool_instance, "_attach_agent", None)
                if not callable(attach_agent):
                    attach_agent = getattr(tool_instance, "attach_agent", None)
                if callable(attach_agent):
                    attach_agent(agent)
                    injected += 1
                elif hasattr(tool_instance, "host_agent"):
                    tool_instance.host_agent = agent
                    injected += 1
        return injected
