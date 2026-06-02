from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from xenon_core.phase_policy import normalize_phase_state, router_phase_for


@dataclass
class ToolRoute:
    intent: str
    phase: str
    candidate_modules: List[str]
    candidate_tools: List[str]
    reasoning_summary: str


class ToolRouter:
    MODULE_RULES: Dict[str, Dict[str, List[str]]] = {
        "file_manager": {
            "keywords": ["file", "folder", "dir", "directory", "path", "search", "find", "locate", "list"],
            "phases": ["analyze", "locate", "maintenance"],
        },
        "code_navigator": {
            "keywords": ["code", "class", "function", "module", "structure", "入口", "导航", "scan"],
            "phases": ["analyze", "locate", "debug"],
        },
        "code_editor_handler": {
            "keywords": ["edit", "modify", "change", "fix", "patch", "write", "replace", "重构", "修改"],
            "phases": ["edit", "debug"],
        },
        "terminal_handler": {
            "keywords": ["run", "command", "shell", "powershell", "test", "build", "install", "执行"],
            "phases": ["test", "debug", "deploy", "maintenance"],
        },
        "debug_handler": {
            "keywords": ["debug", "trace", "breakpoint", "stack", "exception", "报错", "崩溃"],
            "phases": ["debug", "test"],
        },
        "ssh_handler": {
            "keywords": ["ssh", "remote", "server", "deploy", "sftp", "远程", "主机"],
            "phases": ["deploy", "debug"],
        },
        "memory_query_handler": {
            "keywords": ["memory", "recall", "remember", "history", "summary", "记忆"],
            "phases": ["analyze", "reflect", "maintenance"],
        },
        "task_chain_handler": {
            "keywords": ["task", "plan", "step", "progress", "workflow", "任务", "步骤"],
            "phases": ["analyze", "reflect", "maintenance"],
        },
        "context_manager_tool": {
            "keywords": ["context", "token", "summary", "compress", "上下文"],
            "phases": ["analyze", "maintenance", "reflect"],
        },
        "txt_handler": {
            "keywords": ["txt", "text", "note", "文档", "材料", "文本"],
            "phases": ["analyze", "edit"],
        },
        "video_handler": {
            "keywords": [
                "video",
                "audio",
                "ffmpeg",
                "clip",
                "trim",
                "split",
                "merge",
                "concat",
                "thumbnail",
                "gif",
                "transcode",
                "compress",
                "extract audio",
                "remove audio",
                "剪视频",
                "视频",
                "音频",
                "切分",
                "分割",
                "合并",
                "拼接",
                "混音",
                "配音",
                "转码",
                "压缩",
                "缩略图",
            ],
            "phases": ["analyze", "edit", "test", "maintenance"],
        },
    }

    DEFAULT_MODULES = [
        "file_manager",
        "code_navigator",
        "code_editor_handler",
        "terminal_handler",
        "context_manager_tool",
    ]

    VALID_PHASES = {"analyze", "locate", "edit", "test", "debug", "deploy", "reflect", "maintenance"}
    VALID_INTENTS = {
        "general_execution",
        "fix_and_verify",
        "integrate_change",
        "analyze_codebase",
        "remote_operation",
    }

    # 经验教训影响工具排序的惩罚/奖励分值
    LESSON_PENALTY_SCORE = -5      # 失败工具降权
    LESSON_BONUS_SCORE = 2         # 已验证替代工具升权

    def infer_phase(self, user_input: str, current_task: Optional[dict] = None) -> str:
        if current_task:
            execution_state = (current_task.get("execution_state", {}) or {})
            phase = execution_state.get("phase")
            if phase:
                return router_phase_for(phase, execution_state.get("recovery_mode"))

        text = (user_input or "").lower()
        if any(word in text for word in ["debug", "trace", "报错", "异常", "crash", "bug"]):
            return "debug"
        if any(word in text for word in ["test", "pytest", "验证", "检查"]):
            return "test"
        if any(word in text for word in ["deploy", "上线", "ssh", "server", "远程"]):
            return "deploy"
        if any(word in text for word in ["edit", "modify", "change", "fix", "patch", "修改", "集成"]):
            return "edit"
        if any(word in text for word in ["locate", "find", "search", "scan", "入口", "定位", "查找"]):
            return "locate"
        return "analyze"

    def infer_intent(self, user_input: str) -> str:
        text = (user_input or "").lower()
        if any(word in text for word in ["debug", "fix", "报错", "异常", "bug"]):
            return "fix_and_verify"
        if any(word in text for word in ["集成", "integrate", "接入", "落地", "实现"]):
            return "integrate_change"
        if any(word in text for word in ["structure", "scan", "analyze", "阅读", "理解"]):
            return "analyze_codebase"
        if any(word in text for word in ["deploy", "上线", "发布", "server", "远程"]):
            return "remote_operation"
        return "general_execution"

    def route(
        self,
        user_input: str,
        tool_schemas: List[dict],
        current_task: Optional[dict] = None,
        route_hint: Optional[Dict[str, Any]] = None,
        recent_lessons: Optional[List[Dict[str, Any]]] = None,
    ) -> ToolRoute:
        rule_phase = self.infer_phase(user_input, current_task=current_task)
        rule_intent = self.infer_intent(user_input)
        text = (user_input or "").lower()

        module_scores: Dict[str, int] = {}
        for module_name, rule in self.MODULE_RULES.items():
            score = 0
            if rule_phase in rule["phases"]:
                score += 2
            score += sum(2 for keyword in rule["keywords"] if keyword.lower() in text)
            if score > 0:
                module_scores[module_name] = score

        if not module_scores:
            module_scores = {module: 1 for module in self.DEFAULT_MODULES}

        # ── 经验教训影响模块排序 ──
        module_scores = self._apply_lesson_module_adjustments(
            module_scores=module_scores,
            recent_lessons=recent_lessons,
        )

        rule_candidate_modules = [
            module_name
            for module_name, _ in sorted(module_scores.items(), key=lambda item: (-item[1], item[0]))
        ][:5]

        hinted_phase = self._normalize_phase((route_hint or {}).get("phase"))
        hinted_intent = self._normalize_intent((route_hint or {}).get("intent"))
        phase = hinted_phase or rule_phase
        intent = hinted_intent or rule_intent

        hinted_modules = self._normalize_module_candidates(
            (route_hint or {}).get("candidate_modules"),
            tool_schemas=tool_schemas,
        )
        candidate_modules = self._merge_unique(hinted_modules, rule_candidate_modules, self.DEFAULT_MODULES)[:5]

        rule_candidate_tools = self._pick_candidate_tools(
            candidate_modules=candidate_modules,
            tool_schemas=tool_schemas,
            phase=phase,
            text=text,
            recent_lessons=recent_lessons,
        )
        hinted_tools = self._normalize_tool_candidates(
            (route_hint or {}).get("candidate_tools"),
            tool_schemas=tool_schemas,
            candidate_modules=candidate_modules,
        )
        candidate_tools = self._merge_unique(hinted_tools, rule_candidate_tools)[:6]

        reasoning = f"intent={intent}, phase={phase}, modules={', '.join(candidate_modules[:3]) or 'none'}"
        if route_hint:
            confidence = route_hint.get("confidence")
            hint_reasoning = str(route_hint.get("reasoning") or "").strip()
            reasoning += (
                f", route_strategy={'semantic+rule' if hinted_modules or hinted_tools or hinted_phase or hinted_intent else 'rule'}"
            )
            if confidence is not None:
                reasoning += f", semantic_confidence={confidence}"
            if hint_reasoning:
                reasoning += f", semantic_reasoning={hint_reasoning[:160]}"

        # 标注经验教训影响
        if recent_lessons:
            lesson_types = set()
            for lesson in recent_lessons:
                lt = lesson.get("type", "")
                if lt:
                    lesson_types.add(lt)
            if lesson_types:
                reasoning += f", lesson_types={','.join(sorted(lesson_types))}"

        return ToolRoute(
            intent=intent,
            phase=phase,
            candidate_modules=candidate_modules,
            candidate_tools=candidate_tools,
            reasoning_summary=reasoning,
        )

    def _apply_lesson_module_adjustments(
        self,
        module_scores: Dict[str, int],
        recent_lessons: Optional[List[Dict[str, Any]]],
    ) -> Dict[str, int]:
        """基于经验教训调整模块评分：失败工具降权，替代工具升权。"""
        if not recent_lessons:
            return module_scores

        penalized_modules: set = set()
        boosted_modules: set = set()

        for lesson in recent_lessons:
            lesson_type = str(lesson.get("type", "")).strip()
            tool_name = str(lesson.get("tool_name", "")).strip()

            # 失败教训 → 降低对应模块评分
            if lesson_type == "tool_failure" and tool_name:
                for module_name in self.MODULE_RULES:
                    if tool_name.startswith(module_name + "_") or module_name in tool_name:
                        penalized_modules.add(module_name)
                        break

            # 高失败率/阻塞重复 → 降低当前失败工具对应的模块
            if lesson_type in ("high_failure_rate", "blockage_repeat") and tool_name:
                for module_name in self.MODULE_RULES:
                    if tool_name.startswith(module_name + "_") or module_name in tool_name:
                        penalized_modules.add(module_name)
                        break

        # 对惩罚模块降权
        for module_name in penalized_modules:
            if module_name in module_scores:
                module_scores[module_name] = module_scores[module_name] + self.LESSON_PENALTY_SCORE
                # 不要让分数低于 0
                module_scores[module_name] = max(0, module_scores[module_name])

        # 对非惩罚模块升权（作为替代路径）
        if penalized_modules:
            for module_name in module_scores:
                if module_name not in penalized_modules:
                    module_scores[module_name] = module_scores[module_name] + self.LESSON_BONUS_SCORE
                    boosted_modules.add(module_name)

        return module_scores

    def _pick_candidate_tools(
        self,
        candidate_modules: List[str],
        tool_schemas: List[dict],
        phase: str,
        text: str,
        recent_lessons: Optional[List[Dict[str, Any]]] = None,
    ) -> List[str]:
        scored_tools = []
        for schema in tool_schemas:
            function = schema.get("function", {})
            tool_name = function.get("name", "")
            module_name = self._match_module(tool_name, candidate_modules)
            if not module_name:
                continue

            score = 1
            lowered = tool_name.lower()

            if phase in {"analyze", "locate"} and any(token in lowered for token in ["view", "read", "search", "scan", "list"]):
                score += 3
            if phase == "edit" and any(token in lowered for token in ["edit", "write", "replace", "create", "update"]):
                score += 3
            if phase in {"test", "debug"} and any(token in lowered for token in ["test", "run", "execute", "debug", "trace"]):
                score += 3
            if phase == "deploy" and any(token in lowered for token in ["ssh", "sftp", "remote", "upload", "deploy"]):
                score += 3

            score += sum(
                1
                for token in ["search", "find", "view", "read", "edit", "execute"]
                if token in lowered and token in text
            )

            # ── 经验教训影响工具评分 ──
            if module_name == "video_handler":
                if any(token in text for token in ["video", "audio", "视频", "音频", "剪视频"]):
                    score += 2
                if any(token in text for token in ["split", "cut", "trim", "切分", "分割", "剪"]):
                    if any(token in lowered for token in ["split", "trim"]):
                        score += 4
                if any(token in text for token in ["merge", "concat", "combine", "合并", "拼接", "组合"]):
                    if any(token in lowered for token in ["merge", "concat"]):
                        score += 4
                if any(token in text for token in ["mix", "dub", "voice", "audio", "混音", "配音", "音频"]):
                    if "audio" in lowered:
                        score += 4
                if any(token in text for token in ["thumbnail", "cover", "缩略图", "封面"]):
                    if "thumbnail" in lowered:
                        score += 4
                if "gif" in text and "gif" in lowered:
                    score += 4

            if recent_lessons:
                score = self._apply_lesson_tool_score(tool_name, score, recent_lessons)

            scored_tools.append((tool_name, score))

        if not scored_tools:
            return []

        ranked = sorted(scored_tools, key=lambda item: (-item[1], item[0]))
        unique_tools: List[str] = []
        for tool_name, _ in ranked:
            if tool_name not in unique_tools:
                unique_tools.append(tool_name)
            if len(unique_tools) >= 6:
                break
        return unique_tools

    def _apply_lesson_tool_score(
        self,
        tool_name: str,
        base_score: int,
        recent_lessons: List[Dict[str, Any]],
    ) -> int:
        """基于经验教训调整单个工具评分。"""
        for lesson in recent_lessons:
            lesson_type = str(lesson.get("type", "")).strip()
            lesson_tool = str(lesson.get("tool_name", "")).strip()

            if lesson_type == "tool_failure" and lesson_tool:
                if tool_name.startswith(lesson_tool) or lesson_tool in tool_name:
                    base_score += self.LESSON_PENALTY_SCORE

            if lesson_type in ("high_failure_rate", "blockage_repeat") and lesson_tool:
                if tool_name.startswith(lesson_tool) or lesson_tool in tool_name:
                    base_score += self.LESSON_PENALTY_SCORE

        return max(0, base_score)

    @staticmethod
    def _match_module(tool_name: str, candidate_modules: List[str]) -> Optional[str]:
        for module_name in candidate_modules:
            if tool_name.startswith(module_name + "_"):
                return module_name
        return None

    @classmethod
    def _normalize_phase(cls, value: Any) -> Optional[str]:
        text = str(value or "").strip().lower()
        if text in cls.VALID_PHASES:
            return text
        normalized = normalize_phase_state(text)
        routed = router_phase_for(normalized["phase"], normalized["recovery_mode"])
        return routed if routed in cls.VALID_PHASES else None

    @classmethod
    def _normalize_intent(cls, value: Any) -> Optional[str]:
        text = str(value or "").strip().lower()
        return text if text in cls.VALID_INTENTS else None

    def _normalize_module_candidates(self, values: Any, tool_schemas: List[dict]) -> List[str]:
        if not isinstance(values, list):
            return []

        normalized: List[str] = []
        for value in values:
            module_name = str(value or "").strip()
            if module_name and self._module_exists(module_name, tool_schemas):
                normalized.append(module_name)
        return self._merge_unique(normalized)

    def _normalize_tool_candidates(
        self,
        values: Any,
        tool_schemas: List[dict],
        candidate_modules: List[str],
    ) -> List[str]:
        if not isinstance(values, list):
            return []

        available_tools = {(schema.get("function", {}) or {}).get("name", "") for schema in tool_schemas}
        normalized: List[str] = []
        for value in values:
            tool_name = str(value or "").strip()
            if not tool_name or tool_name not in available_tools:
                continue
            if candidate_modules and not self._match_module(tool_name, candidate_modules):
                continue
            normalized.append(tool_name)
        return self._merge_unique(normalized)

    @staticmethod
    def _merge_unique(*lists: List[str]) -> List[str]:
        merged: List[str] = []
        for values in lists:
            for value in values:
                if value and value not in merged:
                    merged.append(value)
        return merged

    @staticmethod
    def _module_exists(module_name: str, tool_schemas: List[dict]) -> bool:
        for schema in tool_schemas:
            tool_name = (schema.get("function", {}) or {}).get("name", "")
            if tool_name.startswith(module_name + "_"):
                return True
        return False
