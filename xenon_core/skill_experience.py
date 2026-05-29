from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_SKILLS_DIR = Path("Memory") / "skills"
SKILL_INDEX_FILENAME = "skill_index.json"


class SkillExperienceManager:
    """Promote repeated execution lessons into reusable workflow skill cards."""

    def __init__(
        self,
        *,
        skills_dir: Optional[Any] = None,
        promotion_min_count: int = 3,
        promotion_min_weight: float = 2.0,
        max_fragment_chars: int = 1400,
    ) -> None:
        self.skills_dir = Path(skills_dir) if skills_dir else DEFAULT_SKILLS_DIR
        self.index_path = self.skills_dir / SKILL_INDEX_FILENAME
        self.promotion_min_count = max(1, int(promotion_min_count))
        self.promotion_min_weight = max(0.0, float(promotion_min_weight))
        self.max_fragment_chars = max_fragment_chars

    def promote_from_experience_store(self, experience_store: Dict[str, Any]) -> List[Dict[str, Any]]:
        promoted: List[Dict[str, Any]] = []
        for entry in experience_store.get("lessons", []) or []:
            if not isinstance(entry, dict) or not self._is_promotable(entry):
                continue
            promoted.append(self.upsert_skill_from_lesson(entry))
        return promoted

    def upsert_skill_from_lesson(self, lesson_entry: Dict[str, Any]) -> Dict[str, Any]:
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        index = self.load_index()

        skill = self._build_skill_record(lesson_entry)
        existing = index.get(skill["skill_id"], {})
        merged = {**existing, **skill}
        merged["created_at"] = existing.get("created_at") or skill["created_at"]
        merged["updated_at"] = datetime.now().isoformat()
        merged["usage_count"] = int(existing.get("usage_count", 0))

        skill_path = self.skills_dir / f"{merged['skill_id']}.md"
        merged["path"] = str(skill_path)
        skill_path.write_text(self._render_skill_markdown(merged), encoding="utf-8")

        index[merged["skill_id"]] = merged
        self.save_index(index)
        return merged

    def search_skills(
        self,
        query: str,
        *,
        current_phase: str = "",
        candidate_tools: Optional[List[str]] = None,
        current_failure_type: str = "",
        limit: int = 3,
    ) -> List[Dict[str, Any]]:
        index = self.load_index()
        if not index:
            return []

        query_tokens = set(_tokenize(query))
        tool_tokens = set(_tokenize(" ".join(candidate_tools or [])))
        scored: List[tuple[float, Dict[str, Any]]] = []
        for skill in index.values():
            score = self._score_skill(
                skill,
                query_tokens=query_tokens,
                current_phase=current_phase,
                tool_tokens=tool_tokens,
                current_failure_type=current_failure_type,
            )
            if score > 0:
                scored.append((score, skill))

        scored.sort(key=lambda item: (-item[0], item[1].get("title", "")))
        return [skill for _, skill in scored[: max(1, limit)]]

    def build_prompt_fragment(
        self,
        query: str,
        *,
        current_phase: str = "",
        candidate_tools: Optional[List[str]] = None,
        current_failure_type: str = "",
        limit: int = 3,
        max_chars: Optional[int] = None,
    ) -> str:
        skills = self.search_skills(
            query,
            current_phase=current_phase,
            candidate_tools=candidate_tools,
            current_failure_type=current_failure_type,
            limit=limit,
        )
        if not skills:
            return ""

        lines = [
            "[SKILL_EXPERIENCE]",
            "- Apply these learned workflow cards before choosing tools or repeating failed paths.",
        ]
        for skill in skills:
            lines.extend(
                [
                    f"### {skill.get('title', skill.get('skill_id'))}",
                    f"- when: {skill.get('trigger', '')}",
                    f"- workflow: {' -> '.join(skill.get('workflow', [])[:4])}",
                    f"- avoid: {'; '.join(skill.get('avoid', [])[:3]) or 'none'}",
                    f"- verify: {'; '.join(skill.get('verification', [])[:3])}",
                    f"- file: {skill.get('path', '')}",
                ]
            )

        return _truncate("\n".join(lines), max_chars or self.max_fragment_chars)

    def load_index(self) -> Dict[str, Dict[str, Any]]:
        if not self.index_path.exists():
            return {}
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return {
                    key: value
                    for key, value in payload.get("skills", payload).items()
                    if isinstance(value, dict)
                }
        except Exception:
            return {}
        return {}

    def save_index(self, index: Dict[str, Dict[str, Any]]) -> None:
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now().isoformat(),
            "skills": index,
        }
        self.index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def record_skill_usage(self, skill_ids: Iterable[str]) -> None:
        index = self.load_index()
        changed = False
        for skill_id in skill_ids:
            if skill_id in index:
                index[skill_id]["usage_count"] = int(index[skill_id].get("usage_count", 0)) + 1
                index[skill_id]["last_used_at"] = datetime.now().isoformat()
                changed = True
        if changed:
            self.save_index(index)

    def _is_promotable(self, entry: Dict[str, Any]) -> bool:
        count = int(entry.get("count", 1) or 1)
        weight = float(entry.get("weight", 1.0) or 1.0)
        lesson_type = str(entry.get("type", "") or "")
        return (
            count >= self.promotion_min_count
            or weight >= self.promotion_min_weight
            or lesson_type in {"tool_failure", "phase_stagnation", "blockage_repeat", "high_failure_rate"}
        )

    def _build_skill_record(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        lesson = str(entry.get("lesson", "")).strip()
        lesson_type = str(entry.get("type") or "general").strip() or "general"
        phase = str(entry.get("phase") or "").strip()
        tool_name = str(entry.get("tool_name") or "").strip()
        failure_type = str(entry.get("failure_type") or "").strip()
        fingerprint = str(entry.get("fingerprint") or "").strip()
        if not fingerprint:
            raw = f"{lesson_type}|{phase}|{tool_name}|{failure_type}|{lesson}"
            fingerprint = hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]

        title = self._build_title(lesson_type, phase, tool_name, failure_type)
        workflow, avoid, verification = self._workflow_for_type(
            lesson_type=lesson_type,
            phase=phase,
            tool_name=tool_name,
            failure_type=failure_type,
        )
        keywords = sorted(
            set(_tokenize(" ".join([title, lesson, lesson_type, phase, tool_name, failure_type])))
        )[:40]

        return {
            "skill_id": f"skill_{fingerprint}",
            "title": title,
            "trigger": self._build_trigger(lesson_type, phase, tool_name, failure_type),
            "lesson": lesson,
            "lesson_type": lesson_type,
            "phase": phase,
            "tool_name": tool_name,
            "failure_type": failure_type,
            "tags": list(entry.get("tags") or [lesson_type]),
            "keywords": keywords,
            "workflow": workflow,
            "avoid": avoid,
            "verification": verification,
            "source": {
                "count": int(entry.get("count", 1) or 1),
                "weight": float(entry.get("weight", 1.0) or 1.0),
                "timestamp": entry.get("timestamp", ""),
                "context": entry.get("context", ""),
            },
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "usage_count": 0,
        }

    @staticmethod
    def _build_title(lesson_type: str, phase: str, tool_name: str, failure_type: str) -> str:
        if lesson_type == "tool_failure" and tool_name:
            return f"工具失败恢复：{tool_name}"
        if lesson_type == "phase_stagnation" and phase:
            return f"阶段停滞恢复：{phase}"
        if lesson_type == "blockage_repeat" and failure_type:
            return f"重复阻塞处理：{failure_type}"
        if lesson_type == "high_failure_rate":
            return "高失败率执行降级"
        if lesson_type == "evidence_gap":
            return "证据不足时的保守回答"
        if lesson_type == "loop_detected":
            return "重复循环熔断"
        if lesson_type == "logic":
            return "逻辑一致性自检"
        if lesson_type == "safety":
            return "专业领域安全提示"
        return f"通用经验：{lesson_type}"

    @staticmethod
    def _build_trigger(lesson_type: str, phase: str, tool_name: str, failure_type: str) -> str:
        parts = [f"type={lesson_type}"]
        if phase:
            parts.append(f"phase={phase}")
        if tool_name:
            parts.append(f"tool={tool_name}")
        if failure_type:
            parts.append(f"failure={failure_type}")
        return ", ".join(parts)

    @staticmethod
    def _workflow_for_type(
        *,
        lesson_type: str,
        phase: str,
        tool_name: str,
        failure_type: str,
    ) -> tuple[List[str], List[str], List[str]]:
        if lesson_type == "tool_failure":
            target = tool_name or "the failing tool"
            return (
                [
                    "Stop blind retries after repeated failure",
                    f"Inspect arguments, path, permissions, and recent output for {target}",
                    "Switch to a safer fallback tool or narrower verification path",
                    "Record the new result and only retry when the failure condition changed",
                ],
                [f"Do not repeatedly call {target} with the same arguments"],
                ["Confirm the fallback produced new evidence", "Check execution state moved forward"],
            )
        if lesson_type == "phase_stagnation":
            return (
                [
                    f"Recognize that phase '{phase or 'current'}' is not progressing",
                    "Summarize what is known and what is blocked",
                    "Switch to reflect/replan before executing another broad action",
                    "Choose one small verifiable next action",
                ],
                ["Do not keep expanding analysis without a new decision point"],
                ["Next action is concrete", "Phase or blockage reason changed"],
            )
        if lesson_type == "blockage_repeat":
            return (
                [
                    f"Treat repeated blockage '{failure_type or 'unknown'}' as a strategy failure",
                    "Stop retrying the same recovery path",
                    "Change tool, scope, or ask for missing external input",
                    "Update task state with the chosen recovery path",
                ],
                ["Do not treat repeated blockage as a transient one-off failure"],
                ["Blockage reason is resolved or explicitly escalated"],
            )
        if lesson_type == "high_failure_rate":
            return (
                [
                    "Pause execution and enter reflect mode",
                    "Cluster recent failures by tool and phase",
                    "Pick the lowest-risk fallback path",
                    "Run a small validation before resuming full execution",
                ],
                ["Do not continue autonomous execution at the same pace"],
                ["Failure rate drops after fallback", "Recovery plan is recorded"],
            )
        if lesson_type == "evidence_gap":
            return (
                [
                    "Compare answer strength against available tool evidence",
                    "Mark unsupported claims as tentative",
                    "Gather source or numeric evidence before strong conclusions",
                    "State the evidence gap clearly if validation is incomplete",
                ],
                ["Do not claim verification without usable tool evidence"],
                ["Every precise claim has a source or lowered confidence"],
            )
        if lesson_type == "loop_detected":
            return (
                [
                    "Detect repeated wording or repeated failed reasoning",
                    "Switch perspective or method immediately",
                    "Use a new tool path or ask for a boundary decision",
                ],
                ["Do not repeat the same reasoning chain"],
                ["New action differs from the previous loop"],
            )
        if lesson_type == "logic":
            return (
                [
                    "Scan for mutually conflicting statements",
                    "Downgrade certainty when premises conflict",
                    "Resolve or explicitly name the contradiction before final output",
                ],
                ["Do not preserve both sides as confirmed conclusions"],
                ["Final answer has one consistent stance or a clear uncertainty note"],
            )
        if lesson_type == "safety":
            return (
                [
                    "Identify the professional domain",
                    "Provide a conservative answer",
                    "Add an appropriate disclaimer and recommend expert confirmation",
                ],
                ["Do not present professional advice as definitive"],
                ["Disclaimer is visible and matched to the domain"],
            )
        return (
            ["Review the lesson", "Apply the safest relevant workflow", "Verify before finalizing"],
            ["Do not ignore repeated lessons"],
            ["Outcome is verified and lesson-compatible"],
        )

    @staticmethod
    def _render_skill_markdown(skill: Dict[str, Any]) -> str:
        source = skill.get("source", {}) or {}
        lines = [
            f"# {skill.get('title', 'Xenon Skill')}",
            "",
            "<!-- xenon_skill_card: v1 -->",
            f"- skill_id: {skill.get('skill_id')}",
            f"- trigger: {skill.get('trigger')}",
            f"- tags: {', '.join(skill.get('tags') or [])}",
            f"- source_count: {source.get('count')}",
            f"- source_weight: {source.get('weight')}",
            f"- updated_at: {skill.get('updated_at')}",
            "",
            "## Lesson",
            skill.get("lesson", ""),
            "",
            "## Workflow",
        ]
        lines.extend(f"1. {item}" for item in skill.get("workflow", []))
        lines.append("")
        lines.append("## Avoid")
        lines.extend(f"- {item}" for item in skill.get("avoid", []))
        lines.append("")
        lines.append("## Verification")
        lines.extend(f"- {item}" for item in skill.get("verification", []))
        lines.append("")
        lines.append("## Source Context")
        lines.append(str(source.get("context", "")))
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _score_skill(
        skill: Dict[str, Any],
        *,
        query_tokens: set[str],
        current_phase: str,
        tool_tokens: set[str],
        current_failure_type: str,
    ) -> float:
        source = skill.get("source", {}) or {}
        score = min(float(source.get("weight", 1.0) or 1.0), 12.0) * 0.25
        score += min(int(source.get("count", 1) or 1), 20) * 0.1

        if current_phase and skill.get("phase") == current_phase:
            score += 4.0
        if current_failure_type and skill.get("failure_type") == current_failure_type:
            score += 4.0

        skill_tokens = set(skill.get("keywords") or [])
        score += min(len(query_tokens & skill_tokens), 8) * 0.7
        if tool_tokens:
            score += min(len(tool_tokens & skill_tokens), 6) * 0.8

        lesson_type = skill.get("lesson_type")
        if current_phase in {"debug", "test"} and lesson_type in {"tool_failure", "high_failure_rate"}:
            score += 1.5
        if current_phase in {"analyze", "reflect"} and lesson_type in {"phase_stagnation", "loop_detected"}:
            score += 1.5
        return score


def _tokenize(text: str) -> List[str]:
    normalized = str(text or "").lower()
    tokens = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{1,8}", normalized)
    return [token for token in tokens if token.strip()]


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = "\n...[skill guidance truncated]"
    return text[: max(0, max_chars - len(marker))].rstrip() + marker
