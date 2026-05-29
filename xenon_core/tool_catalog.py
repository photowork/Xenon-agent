from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set


def authorize_single_tool(
    loaded_single_tools: Dict[str, Dict[str, Any]],
    approved_tools: Set[str],
    tool_name: str,
    schema: Dict[str, Any],
    *,
    now_fn: Callable[[], Any] = datetime.now,
) -> None:
    now = now_fn()
    loaded_single_tools[tool_name] = {
        "schema": schema,
        "last_used": now,
        "loaded_at": now,
        "source": "get_tool_description",
    }
    approved_tools.add(tool_name)


def touch_single_tool(
    loaded_single_tools: Dict[str, Dict[str, Any]],
    tool_name: str,
    *,
    now_fn: Callable[[], Any] = datetime.now,
) -> None:
    if tool_name in loaded_single_tools:
        loaded_single_tools[tool_name]["last_used"] = now_fn()


def is_single_tool_loaded(
    loaded_single_tools: Dict[str, Dict[str, Any]],
    tool_name: str,
) -> bool:
    return tool_name in loaded_single_tools


def is_tool_loaded(
    loaded_modules: Dict[str, Dict[str, Any]],
    tool_name: str,
    *,
    now_fn: Callable[[], Any] = datetime.now,
) -> bool:
    for module_info in loaded_modules.values():
        if tool_name in module_info.get("tool_names", set()):
            module_info["last_used"] = now_fn()
            return True
    return False


def build_current_tools(
    *,
    load_module_tool: Dict[str, Any],
    tool_description_tool: Dict[str, Any],
    get_module_tools_tool: Dict[str, Any],
    loaded_modules: Dict[str, Dict[str, Any]],
    loaded_single_tools: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    tools = [load_module_tool, tool_description_tool, get_module_tools_tool]
    for module_info in loaded_modules.values():
        tools.extend(module_info.get("tools", []))

    for tool_name, tool_info in loaded_single_tools.items():
        already_in_module = any(
            tool_name in module_info.get("tool_names", set())
            for module_info in loaded_modules.values()
        )
        if not already_in_module:
            schema = tool_info.get("schema")
            if schema:
                tools.append(schema)
    return tools


def unload_module(
    loaded_modules: Dict[str, Dict[str, Any]],
    loaded_single_tools: Dict[str, Dict[str, Any]],
    approved_tools: Set[str],
    module_name: str,
) -> Optional[str]:
    if module_name not in loaded_modules:
        return None

    module_info = loaded_modules.pop(module_name)
    tool_names = module_info.get("tool_names", set())
    for tool_name in tool_names:
        if tool_name not in loaded_single_tools:
            approved_tools.discard(tool_name)
    return f"\033[93m[模块卸载] {module_name} ({len(tool_names)} 个工具已移除)\033[0m"


def reset_loaded_tools_for_new_turn(
    loaded_modules: Dict[str, Dict[str, Any]],
    loaded_single_tools: Dict[str, Dict[str, Any]],
    approved_tools: Set[str],
    *,
    print_fn: Callable[..., Any] = print,
) -> Optional[str]:
    loaded_tool_names: Set[str] = set()
    for module_info in loaded_modules.values():
        loaded_tool_names.update(module_info.get("tool_names", set()))
    loaded_tool_names.update(loaded_single_tools.keys())

    module_count = len(loaded_modules)
    single_tool_count = len(loaded_single_tools)
    if not module_count and not single_tool_count:
        return None

    loaded_modules.clear()
    loaded_single_tools.clear()
    approved_tools.difference_update(loaded_tool_names)
    message = (
        f"[工具状态] 上一轮临时工具授权已释放：模块 {module_count} 个，"
        f"单工具 {single_tool_count} 个。后续如需使用业务工具，请先重新加载对应模块或单工具。"
    )
    print_fn(f"\033[93m{message}\033[0m")
    return message


def handle_get_tool_description_call(
    *,
    tool_call_id: str,
    arguments_str: str,
    messages: List[Dict[str, Any]],
    parse_arguments_fn: Callable[[str], Dict[str, Any]],
    get_tool_schema_by_name_fn: Callable[[str], Optional[Dict[str, Any]]],
    authorize_single_tool_fn: Callable[[str, Dict[str, Any]], None],
    add_tool_message_fn: Callable[[List[Dict[str, Any]], str, str], None],
    handle_tool_error_fn: Callable[[List[Dict[str, Any]], str, Exception, str], None],
    logger: Any,
    print_fn: Callable[..., Any] = print,
) -> None:
    try:
        arguments = parse_arguments_fn(arguments_str)
        target_tool_name = arguments.get("tool_name", "")
        print_fn(
            f"\n\033[38;2;195;197;64m获取工具描述: \033[0m\033[38;2;195;197;64m{target_tool_name}\033[0m"
        )

        tool_schema = get_tool_schema_by_name_fn(target_tool_name)
        if not tool_schema:
            error_msg = f"错误：工具 '{target_tool_name}' 不存在。请检查工具名称是否正确。"
            print_fn(f"\n\033[91m{error_msg}\033[0m\n")
            add_tool_message_fn(messages, tool_call_id, error_msg)
            return

        authorize_single_tool_fn(target_tool_name, tool_schema)
        response = build_tool_description_response(target_tool_name, tool_schema)
        print_fn(f"\n\033[38;2;195;197;64m{response}\033[0m\n")
        add_tool_message_fn(messages, tool_call_id, response)

    except json.JSONDecodeError as error:
        logger.error("JSON解析失败: %s", error)
        logger.error("原始参数字符串: %r", arguments_str)
        error_msg = f"JSON解析错误: {error}\n原始参数: {arguments_str[:200]}..."
        print_fn(f"错误: 参数格式错误 - {error}")
        add_tool_message_fn(messages, tool_call_id, error_msg)
    except Exception as error:
        handle_tool_error_fn(messages, tool_call_id, error, "获取工具描述")


def handle_get_module_tools_call(
    *,
    tool_call_id: str,
    arguments_str: str,
    messages: List[Dict[str, Any]],
    parse_arguments_fn: Callable[[str], Dict[str, Any]],
    get_tool_list_fn: Callable[[], List[str]],
    add_tool_message_fn: Callable[[List[Dict[str, Any]], str, str], None],
    handle_tool_error_fn: Callable[[List[Dict[str, Any]], str, Exception, str], None],
    logger: Any,
    print_fn: Callable[..., Any] = print,
) -> None:
    try:
        arguments = parse_arguments_fn(arguments_str)
        target_module_name = arguments.get("module_name", "")
        print_fn(
            f"\n\033[38;2;195;197;64m获取模块工具列表: \033[0m\033[38;2;195;197;64m{target_module_name}\033[0m"
        )

        tool_list = get_tool_list_fn()
        module_tools = [name for name in tool_list if name.startswith(target_module_name)]
        if not module_tools:
            error_msg = f"错误：模块 '{target_module_name}' 不存在或没有工具。"
            print_fn(f"\n\033[91m{error_msg}\033[0m\n")
            add_tool_message_fn(messages, tool_call_id, error_msg)
            return

        response = build_module_tools_response(target_module_name, module_tools)
        print_fn(f"\n\033[38;2;195;197;64m{response}\033[0m\n")
        add_tool_message_fn(messages, tool_call_id, response)

    except json.JSONDecodeError as error:
        logger.error("JSON解析失败: %s", error)
        logger.error("原始参数字符串: %r", arguments_str)
        error_msg = f"JSON解析错误: {error}\n原始参数: {arguments_str[:200]}..."
        print_fn(f"错误: 参数格式错误 - {error}")
        add_tool_message_fn(messages, tool_call_id, error_msg)
    except Exception as error:
        handle_tool_error_fn(messages, tool_call_id, error, "获取模块工具列表")


def handle_load_module_call(
    *,
    tool_call_id: str,
    arguments_str: str,
    messages: List[Dict[str, Any]],
    parse_arguments_fn: Callable[[str], Dict[str, Any]],
    get_tool_list_fn: Callable[[], List[str]],
    get_tool_schema_by_name_fn: Callable[[str], Optional[Dict[str, Any]]],
    add_tool_message_fn: Callable[[List[Dict[str, Any]], str, str], None],
    handle_tool_error_fn: Callable[[List[Dict[str, Any]], str, Exception, str], None],
    loaded_modules: Dict[str, Dict[str, Any]],
    loaded_single_tools: Dict[str, Dict[str, Any]],
    approved_tools: Set[str],
    max_loaded_modules: int,
    logger: Any,
    print_fn: Callable[..., Any] = print,
    now_fn: Callable[[], Any] = datetime.now,
) -> None:
    try:
        arguments = parse_arguments_fn(arguments_str)
        module_names = arguments.get("module_names", [])
        if isinstance(module_names, str):
            module_names = [module_names]

        if not module_names:
            error_msg = "错误：请提供至少一个模块名称。"
            print_fn(f"\n\033[91m{error_msg}\033[0m\n")
            add_tool_message_fn(messages, tool_call_id, error_msg)
            return

        print_fn(f"\n\033[38;2;195;197;64m加载模块: \033[0m\033[38;2;195;197;64m{module_names}\033[0m")
        all_tool_list = get_tool_list_fn()
        loaded_info: List[str] = []
        already_loaded: List[str] = []

        for module_name in module_names:
            if module_name in loaded_modules:
                loaded_modules[module_name]["last_used"] = now_fn()
                already_loaded.append(module_name)
                continue

            module_tool_names = [
                name for name in all_tool_list if name.startswith(module_name + "_")
            ]
            if not module_tool_names:
                error_msg = f"模块 '{module_name}' 不存在或没有工具。"
                print_fn(f"\n\033[91m{error_msg}\033[0m")
                loaded_info.append(f"❌ {module_name}: {error_msg}")
                continue

            module_schemas: List[Dict[str, Any]] = []
            for tool_name in module_tool_names:
                schema = get_tool_schema_by_name_fn(tool_name)
                if schema:
                    module_schemas.append(schema)
                    approved_tools.add(tool_name)

            if module_schemas:
                while len(loaded_modules) >= max_loaded_modules:
                    oldest_name = min(
                        loaded_modules,
                        key=lambda name: loaded_modules[name]["last_used"],
                    )
                    message = unload_module(
                        loaded_modules,
                        loaded_single_tools,
                        approved_tools,
                        oldest_name,
                    )
                    if message:
                        print_fn(message)

                loaded_modules[module_name] = {
                    "tools": module_schemas,
                    "tool_names": set(module_tool_names),
                    "last_used": now_fn(),
                }
                loaded_info.append(
                    f"✅ {module_name}: 已加载 {len(module_schemas)} 个工具 "
                    f"({', '.join(module_tool_names)})"
                )

        response_parts: List[str] = []
        if loaded_info:
            response_parts.append("模块加载结果：\n" + "\n".join(loaded_info))
        if already_loaded:
            response_parts.append(
                f"以下模块已在缓存中，已刷新使用时间: {', '.join(already_loaded)}"
            )

        response = "\n\n".join(response_parts)
        response += "\n\n你现在可以直接使用已加载模块中的工具。"
        print_fn(f"\n\033[38;2;195;197;64m{response}\033[0m\n")
        add_tool_message_fn(messages, tool_call_id, response)

    except json.JSONDecodeError as error:
        logger.error("JSON解析失败: %s", error)
        logger.error("原始参数字符串: %r", arguments_str)
        error_msg = f"JSON解析错误: {error}\n原始参数: {arguments_str[:200]}..."
        print_fn(f"错误: 参数格式错误 - {error}")
        add_tool_message_fn(messages, tool_call_id, error_msg)
    except Exception as error:
        handle_tool_error_fn(messages, tool_call_id, error, "加载模块")


def build_tool_description_response(
    target_tool_name: str,
    tool_schema: Dict[str, Any],
) -> str:
    tool_info = tool_schema["function"]
    response = f"工具 '{target_tool_name}' 的详细描述：\n\n"
    response += f"名称：{tool_info['name']}\n"
    response += f"描述：{tool_info['description']}\n"

    if tool_info.get("parameters"):
        params = tool_info["parameters"]
        response += "\n参数：\n"
        for param_name, param_info in params.get("properties", {}).items():
            param_type = param_info.get("type", "unknown")
            param_desc = param_info.get("description", "")
            required = param_name in params.get("required", [])
            required_mark = "（必需）" if required else "（可选）"
            response += f"  - {param_name} [{param_type}]{required_mark}: {param_desc}\n"
    else:
        response += "\n参数：无\n"

    response += "\n现在你可以使用该工具了。请按照参数要求调用该工具。"
    return response


def build_module_tools_response(
    target_module_name: str,
    module_tools: List[str],
) -> str:
    response = f"模块 '{target_module_name}' 下的工具列表：\n\n"
    response += "\n".join(f"{index}. {name}" for index, name in enumerate(module_tools, 1))
    response += "\n\n请使用 get_tool_description 工具获取具体工具的详细描述。"
    return response
