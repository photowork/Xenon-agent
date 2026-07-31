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
    # ── 通用 phase 与模块名关键词的亲和度映射 ──
    # 新增工具只要命名符合 {module}_{ToolName} 格式，就会自动被检测并评分
    PHASE_MODULE_AFFINITY = {
        "analyze": [
            "code", "navigat", "search", "read", "view", "scan",
            "memory", "context", "graph", "knowledge", "vecdb",
            "query", "causal", "reasoner",
        ],
        "locate": ["navigat", "search", "find", "scan"],
        "edit": ["edit", "write", "handler", "code", "replace", "create"],
        "test": ["terminal", "run", "execut", "test", "simul"],
        "debug": ["debug", "trace", "terminal", "code", "navigat", "exception"],
        "deploy": ["ssh", "deploy", "remote", "sftp", "server", "git"],
        "reflect": ["memory", "context", "skill", "soul", "knowledge", "causal"],
        "maintenance": [
            "context", "memory", "skill", "handler",
            "wps", "word", "excel", "pdf", "packager",
        ],
    }

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

    # ─────────────────────────────────────────────
    #  动态模块发现（从 tool_schemas 提取）
    # ─────────────────────────────────────────────

    @staticmethod
    def discover_all_modules(tool_schemas: List[dict]) -> List[str]:
        """从 tool_schemas 动态发现所有可用模块名。

        工具名格式: `{模块名}_{类名}_{方法名}`
        类名首字母大写 (PascalCase)，通过检测第一个大写段来分割。
        从第一个大写段之前提取模块名，支持多段模块名如 `code_editor_handler`。
        """
        modules: List[str] = []
        for schema in tool_schemas or []:
            tool_name = (schema.get("function", {}) or {}).get("name", "")
            parts = tool_name.split("_")
            # 找到第一个首字母大写的段 → 类名起点
            class_idx = None
            for i, part in enumerate(parts):
                if part and part[0].isupper():
                    class_idx = i
                    break
            if class_idx is not None and class_idx > 0:
                module_name = "_".join(parts[:class_idx])
            elif "_" in tool_name:
                # 没有类名段时回退取第一段
                module_name = tool_name.split("_", 1)[0]
            else:
                continue

            if module_name and module_name not in modules:
                modules.append(module_name)
        return modules

    # ─────────────────────────────────────────────
    #  通用模块评分（无需为每个模块单独配置规则）
    # ─────────────────────────────────────────────

    def score_all_modules(
        self,
        tool_schemas: List[dict],
        phase: str,
        text: str,
    ) -> Dict[str, int]:
        """对所有发现的模块进行通用评分。

        策略：
        1. 模块名关键词 vs 当前 phase 亲和度
        2. 模块名中的单词 vs 用户输入关键词匹配
        """
        scores: Dict[str, int] = {}
        all_modules = self.discover_all_modules(tool_schemas)

        for module_name in all_modules:
            score = 0
            lowered_module = module_name.lower()

            # ── 1. phase 亲和度评分 ──
            affinity_tokens = self.PHASE_MODULE_AFFINITY.get(phase, [])
            if any(token in lowered_module for token in affinity_tokens):
                score += 2

            # ── 2. 模块名关键词匹配用户输入 ──
            module_tokens = lowered_module.split("_")
            for token in module_tokens:
                if len(token) > 2 and token in text:
                    score += 2

            if score > 0:
                scores[module_name] = score

        return scores

    # ─────────────────────────────────────────────
    #  Phase / Intent 推断
    # ─────────────────────────────────────────────

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

    # ─────────────────────────────────────────────
    #  路由主入口
    # ─────────────────────────────────────────────

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

        # ── 动态发现所有模块，通用评分 ──
        module_scores = self.score_all_modules(
            tool_schemas=tool_schemas,
            phase=rule_phase,
            text=text,
        )

        # 无匹配时回退到全量模块（给基础分 1，确保新工具也能出现）
        if not module_scores:
            all_modules = self.discover_all_modules(tool_schemas)
            module_scores = {module: 1 for module in all_modules}

        # ── 经验教训影响模块排序 ──
        module_scores = self._apply_lesson_module_adjustments(
            module_scores=module_scores,
            recent_lessons=recent_lessons,
            tool_schemas=tool_schemas,
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

        # 兜底：全量发现的模块（动态，非硬编码）
        all_modules = self.discover_all_modules(tool_schemas)
        candidate_modules = self._merge_unique(hinted_modules, rule_candidate_modules, all_modules)[:5]

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

    # ─────────────────────────────────────────────
    #  经验教训影响
    # ─────────────────────────────────────────────

    def _apply_lesson_module_adjustments(
        self,
        module_scores: Dict[str, int],
        recent_lessons: Optional[List[Dict[str, Any]]],
        tool_schemas: List[dict],
    ) -> Dict[str, int]:
        """基于经验教训调整模块评分：失败工具降权，替代工具升权。"""
        if not recent_lessons:
            return module_scores

        penalized_modules: set = set()
        boosted_modules: set = set()

        # 从 tool_schemas 动态获取所有模块名（替代原先的 MODULE_RULES）
        all_modules = self.discover_all_modules(tool_schemas)

        for lesson in recent_lessons:
            lesson_type = str(lesson.get("type", "")).strip()
            tool_name = str(lesson.get("tool_name", "")).strip()

            if lesson_type in ("tool_failure", "high_failure_rate", "blockage_repeat") and tool_name:
                for module_name in all_modules:
                    if tool_name.startswith(module_name + "_") or module_name in tool_name:
                        penalized_modules.add(module_name)
                        break

        # 对惩罚模块降权
        for module_name in penalized_modules:
            if module_name in module_scores:
                module_scores[module_name] = module_scores[module_name] + self.LESSON_PENALTY_SCORE
                module_scores[module_name] = max(0, module_scores[module_name])

        # 对非惩罚模块升权（作为替代路径）
        if penalized_modules:
            for module_name in module_scores:
                if module_name not in penalized_modules:
                    module_scores[module_name] = module_scores[module_name] + self.LESSON_BONUS_SCORE
                    boosted_modules.add(module_name)

        return module_scores

    # ─────────────────────────────────────────────
    #  工具评分与选择
    # ─────────────────────────────────────────────

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

    # ─────────────────────────────────────────────
    #  工具名 ↔ 模块名 映射
    # ─────────────────────────────────────────────

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
