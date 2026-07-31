from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from xenon_core.self_model import SelfModelManager


# 动态运行时上下文块的统一前缀。组装消息时用它在对话历史中定位并清理上一轮残留的动态 system 消息，
# 避免常规模式下动态信息在 current_context 中累积。
DYNAMIC_RUNTIME_PREFIX = "【运行时动态上下文】"


def load_prompts(*, prompts_dir: Path, logger: Any) -> str:
    prompts_content = ""

    if not prompts_dir.exists():
        logger.warning("prompts 文件夹不存在: %s", prompts_dir)
        return prompts_content

    prompt_files = sorted(prompts_dir.rglob("*"), key=lambda path: str(path).lower())
    for file_path in prompt_files:
        if not file_path.is_file() or file_path.name.startswith("."):
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
            if not content.strip():
                continue

            relative_path = file_path.relative_to(prompts_dir).as_posix()
            prompts_content += f"\n--- [{relative_path}] ---\n"
            prompts_content += content
            if not content.endswith("\n"):
                prompts_content += "\n"
        except Exception as error:
            logger.error("读取 prompts 文件失败 %s: %s", file_path, error)

    return prompts_content


def build_system_prompt(
    *,
    system_prompt_base: str,
    prompts_dir: Path,
    logger: Any,
    self_model_manager: Optional[SelfModelManager] = None,
    include_self_model: bool = False,
) -> str:
    prompts = load_prompts(prompts_dir=prompts_dir, logger=logger)
    if prompts.strip():
        system_prompt = system_prompt_base + "\n" + prompts
    else:
        system_prompt = system_prompt_base + "\n（prompts 文件夹当前为空或不存在）\n"

    if include_self_model:
        system_prompt += _build_self_model_prompt(
            self_model_manager=self_model_manager,
            logger=logger,
            log_errors=True,
        )

    return system_prompt


def _build_self_model_prompt(
    *,
    self_model_manager: Optional[SelfModelManager] = None,
    logger: Any = None,
    log_errors: bool = False,
) -> str:
    try:
        manager = self_model_manager or SelfModelManager()
        parts = []
        fragment = manager.get_prompt_fragment(max_chars=1200)
        if fragment:
            parts.append(f"【运行时自我模型】\n{fragment}")
        return "\n\n" + "\n\n".join(parts) if parts else ""
    except Exception as error:
        if log_errors and logger is not None:
            logger.error("注入自我模型摘要失败: %s", error)
        return ""


def build_runtime_system_messages(
    *,
    system_prompt: str,
    project_root: Path,
    cwd: Path,
    tool_list: List[Any],
    module_names: List[str],
    current_task: Optional[Dict[str, Any]],
    loaded_modules: Dict[str, Dict[str, Any]],
    loaded_single_tools: Dict[str, Dict[str, Any]],
    orchestration_guidance: str,
    self_model_manager: Optional[SelfModelManager] = None,
) -> Tuple[str, str]:
    """组装系统消息，拆分为 (static_content, dynamic_content) 两部分。

    static_content: 纯静态提示词（base + prompts），跨轮不变，放在 messages 最前以命中前缀缓存。
    dynamic_content: 运行时动态信息（文件系统/自我模型/工具状态/编排），每轮可能变化，
                     带统一前缀 DYNAMIC_RUNTIME_PREFIX，放在对话历史之后、最新 user 之前。
                     注意：当前时间信息和上下文 Token 状态不在此处，由 turn_runtime 追加到
                     COGNITIVE_NETWORK 消息末尾（两者每轮必然变化，与 COG 同为尾部动态信号）。
    """
    static_content = system_prompt
    dynamic_content = _build_dynamic_runtime_message(
        project_root=project_root,
        cwd=cwd,
        tool_list=tool_list,
        module_names=module_names,
        loaded_modules=loaded_modules,
        loaded_single_tools=loaded_single_tools,
        orchestration_guidance=orchestration_guidance,
        self_model_manager=self_model_manager,
    )
    return static_content, dynamic_content


def _build_dynamic_runtime_message(
    *,
    project_root: Path,
    cwd: Path,
    tool_list: List[Any],
    module_names: List[str],
    loaded_modules: Dict[str, Dict[str, Any]],
    loaded_single_tools: Dict[str, Dict[str, Any]],
    orchestration_guidance: str,
    self_model_manager: Optional[SelfModelManager] = None,
) -> str:
    """构建动态运行时上下文字符串（含统一前缀）。每轮可能变化，不参与前缀缓存。

    不含当前时间信息和 Token 状态——这两项每轮必然变化，由 turn_runtime 追加到
    COGNITIVE_NETWORK 消息末尾，与 COG 摘要一起作为尾部动态信号。
    """
    sections: List[str] = [DYNAMIC_RUNTIME_PREFIX]

    sections.append(
        "【文件系统信息】\n"
        f"- 项目根目录: {project_root}\n"
        f"- 当前工作目录: {cwd}\n"
        "- 路径解析规则:\n"
        "  * 绝对路径: 直接使用\n"
        f"  * 相对路径: 相对于项目根目录 {project_root}\n"
        "  * code_editor_handler 默认基于项目根目录\n"
        "  * Windows 路径支持 \\\\ 或 /"
    )

    self_model_fragment = _build_self_model_prompt(
        self_model_manager=self_model_manager
    ).strip()
    if self_model_fragment:
        sections.append(self_model_fragment)

    if not tool_list:
        sections.append("当前没有可用工具。")
        return "\n\n".join(sections)

    visible_module_names = list(module_names)
    if "context_manager_tool" not in visible_module_names:
        visible_module_names.append("context_manager_tool")

    tools_info = "以下是当前优先展示的工具模块列表:\n\n" + "\n".join(
        f"{index}. {name}" for index, name in enumerate(visible_module_names, 1)
    )
    sections.append(tools_info)

    if loaded_modules:
        sections.append(
            "【已加载的模块】\n"
            "以下模块的工具已加载，可在当前轮对话内直接使用；下一轮会重新加载:\n\n"
            + "\n".join(
                f"✅ {name} ({len(info['tool_names'])} 个工具, "
                f"最后使用: {info['last_used'].strftime('%H:%M:%S')})"
                for name, info in loaded_modules.items()
            )
        )

    if loaded_single_tools:
        sections.append(
            "【已授权的单工具】\n"
            "以下工具已通过 get_tool_description 单独授权，可在当前轮对话内直接使用；下一轮会重新加载:\n\n"
            + "\n".join(
                f"🔧 {name} (最后使用: {info['last_used'].strftime('%H:%M:%S')})"
                for name, info in loaded_single_tools.items()
            )
        )

    sections.append(
        "【工具使用指引】\n"
        "- 使用 load_module 工具加载模块，加载后即可直接调用该模块的全部工具\n"
        "- 可以一次加载多个模块，如: load_module(['terminal_handler', 'code_editor_handler'])\n"
        "- 已加载的工具可直接调用，无需再获取描述\n"
        "- 工具加载状态只在当前轮对话内保留；进入下一轮对话后需要重新加载"
    )

    if orchestration_guidance:
        sections.append(orchestration_guidance)

    # ── 消息轮询池注入 ──
    try:
        from xenon_core.polling_pool import get_pool as _get_pool

        pool = _get_pool(project_root)
        pending = pool.peek_summary(max_count=10)
        if pending:
            lines = []
            for m in pending:
                icon = "🔴" if m["priority"] >= 2 else "🟡" if m["priority"] >= 1 else "  "
                lines.append(
                    f"  {icon} [{m['source']}/{m['scenario']}] "
                    f"{m['summary'][:80] or m['msg_type']} "
                    f"(pri={m['priority']}, age={m['age']})"
                )
            sections.append(
                "【消息轮询池】\n"
                "以下为异步调用返回的待处理结果，你可通过 pool.pull() 取出处理:\n\n"
                + "\n".join(lines)
                + "\n\n使用方式: pool = get_pool(); msg = pool.pull(); 处理完成后 msg.status 自动标记为 consumed"
            )
    except Exception:
        pass  # 轮询池注入非关键

    return "\n\n".join(sections)


def build_available_tools_message(
    *,
    system_prompt: str,
    project_root: Path,
    cwd: Path,
    tool_list: List[Any],
    module_names: List[str],
    current_task: Optional[Dict[str, Any]],
    loaded_modules: Dict[str, Dict[str, Any]],
    loaded_single_tools: Dict[str, Dict[str, Any]],
    orchestration_guidance: str,
    self_model_manager: Optional[SelfModelManager] = None,
) -> str:
    """兼容接口：返回 static + dynamic 拼接的完整字符串。

    供 webui 的 token 估算等仍期望接收单个字符串的调用方使用。
    运行时组装（turn_runtime）请改用 build_runtime_system_messages 获取拆分后的两部分。
    注意：此接口不含当前时间和 Token 状态（已移至 COGNITIVE_NETWORK 末尾）。
    """
    static_content, dynamic_content = build_runtime_system_messages(
        system_prompt=system_prompt,
        project_root=project_root,
        cwd=cwd,
        tool_list=tool_list,
        module_names=module_names,
        current_task=current_task,
        loaded_modules=loaded_modules,
        loaded_single_tools=loaded_single_tools,
        orchestration_guidance=orchestration_guidance,
        self_model_manager=self_model_manager,
    )
    return static_content + "\n\n" + dynamic_content
