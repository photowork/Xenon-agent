from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from xenon_core.self_model import SelfModelManager


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
    now: Optional[datetime] = None,
    self_model_manager: Optional[SelfModelManager] = None,
) -> str:
    current_time = now or datetime.now()
    weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    time_info = (
        "\n\n【当前时间信息】\n"
        f"- 当前时间: {current_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- 时间戳: {current_time.isoformat()}\n"
        f"- 星期: {weekday_names[current_time.weekday()]}"
    )

    file_system_info = (
        "\n\n【文件系统信息】\n"
        f"- 项目根目录: {project_root}\n"
        f"- 当前工作目录: {cwd}\n"
        "- 路径解析规则:\n"
        "  * 绝对路径: 直接使用\n"
        f"  * 相对路径: 相对于项目根目录 {project_root}\n"
        "  * file_manager 默认基于当前工作目录\n"
        "  * code_editor 默认基于项目根目录\n"
        "  * Windows 路径支持 \\\\ 或 /\n"
    )

    message = system_prompt + time_info + file_system_info

    message += _build_self_model_prompt(self_model_manager=self_model_manager)

    message += "\n\n"
    if not tool_list:
        return message + "当前没有可用工具。"

    visible_module_names = list(module_names)
    if "context_manager_tool" not in visible_module_names:
        visible_module_names.append("context_manager_tool")

    tools_info = "以下是当前优先展示的工具模块列表:\n\n" + "\n".join(
        f"{index}. {name}" for index, name in enumerate(visible_module_names, 1)
    )

    loaded_status = ""
    if loaded_modules:
        loaded_status = (
            "\n\n【已加载的模块】\n"
            "以下模块的工具已加载，可在当前轮对话内直接使用；下一轮会重新加载:\n\n"
            + "\n".join(
                f"✅ {name} ({len(info['tool_names'])} 个工具, "
                f"最后使用: {info['last_used'].strftime('%H:%M:%S')})"
                for name, info in loaded_modules.items()
            )
        )

    single_tool_status = ""
    if loaded_single_tools:
        single_tool_status = (
            "\n\n【已授权的单工具】\n"
            "以下工具已通过 get_tool_description 单独授权，可在当前轮对话内直接使用；下一轮会重新加载:\n\n"
            + "\n".join(
                f"🔧 {name} (最后使用: {info['last_used'].strftime('%H:%M:%S')})"
                for name, info in loaded_single_tools.items()
            )
        )

    usage_guide = (
        "\n\n【工具使用指引】\n"
        "- 使用 load_module 工具加载模块，加载后即可直接调用该模块的全部工具\n"
        "- 可以一次加载多个模块，如: load_module(['terminal_handler', 'file_handler'])\n"
        "- 已加载的工具可直接调用，无需再获取描述\n"
        "- 工具加载状态只在当前轮对话内保留；进入下一轮对话后需要重新加载"
    )

    guidance = "\n\n" + orchestration_guidance if orchestration_guidance else ""
    return message + tools_info + loaded_status + single_tool_status + usage_guide + guidance
