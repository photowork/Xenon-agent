#!/usr/bin/env python3
"""Cognitive Network — reads from Memory/memory_Write and .agent_history
   to build a compact, scored memory summary for system-prompt injection.

   Replaces the old graph-based system that relied on metacognition writes.
"""

from __future__ import annotations

import json
import math
import re
import os
from collections import Counter, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = None  # set by caller if needed

# ── scoring constants ──────────────────────────────────────────
IMPORTANCE_KEYWORDS: Dict[str, float] = {
    # filename/title importance boosters
    "用户信息": 3.0,
    "核心": 2.5,
    "重要": 2.0,
    "关键": 2.0,
    "架构": 2.0,
    "设计": 1.8,
    "规划": 1.8,
    "路线图": 2.0,
    "目标": 2.0,
    "关系": 1.5,
    "身份": 2.5,
    "自我": 2.5,
    "决策": 1.8,
    "总结": 1.2,
    "伙伴": 1.8,
    "指南": 1.5,
    "经验": 1.5,
    "教训": 1.5,
    "限制": 1.5,
    "风险": 1.5,
    "边界": 2.0,
}

# filename patterns to EXCLUDE (tool noise)
EXCLUDE_PATTERNS: List[str] = [
    r"execute_tool", r"tool_call", r"load_module", r"failed during",
    r"failed because", r"code_navigator_", r"file_manager_",
    r"code_editor_", r"terminal_handler_", r"debug_handler_",
    r"web_search_", r"vision_tool_", r"excel_handler_",
    r"word_handler_", r"pdf_handler_", r"chart_handler_",
    r"soul_", r"soul_thoughts",
]

TOOL_NOISE_WORDS = [
    "failed during", "failed because", "tool_call", "load_module",
    "code_navigator", "file_manager", "code_editor",
]

# ── helpers ─────────────────────────────────────────────────────

def _has_exclude_pattern(text: str) -> bool:
    lower = text.lower()
    for pat in EXCLUDE_PATTERNS:
        if re.search(pat, lower):
            return True
    return False


def _filename_date(fname: str) -> Optional[datetime]:
    """Parse YYYY-MM-DD or YYYYMMDD from filename."""
    m = re.search(r"(\d{4})[-_]?(\d{2})[-_]?(\d{2})", fname)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            pass
    return None


def _now() -> datetime:
    return datetime.now()


def _days_since(dt: Optional[datetime]) -> Optional[float]:
    if dt is None:
        return None
    return max((_now() - dt).total_seconds() / 86400.0, 0.0)


# ── Core class ──────────────────────────────────────────────────

class CognitiveNetworkState:
    """Load memories from memory_Write and .agent_history, score,
    rank, and produce compact summaries for context injection."""

    def __init__(self, network_path: str = "Memory/memory_network.json"):
        self._cache_path = Path(network_path)  # reuse path for scored index cache
        self._write_dir = Path("Memory/memory_Write")
        self._hist_dir = Path(".agent_history")

        # in-memory index
        self._entries: List[Dict[str, Any]] = []   # scored memory entries
        self._cached_mtime_map: Dict[str, float] = {}  # file → mtime

        # activation tracking (keep from old API)
        self._activation_usage_history: deque = deque(maxlen=10)
        self._last_activation_set: List[Dict[str, Any]] = []

        self._build_index()

    # ── public API (compatible with old CognitiveNetworkState) ──

    def exists(self) -> bool:
        return len(self._entries) > 0

    def build_summary(
        self,
        current_query: Optional[str] = None,
        max_nodes: int = 10,
        max_chars: int = 2600,
    ) -> str:
        if not self._entries:
            return ""

        ranked = self._rank(self._entries, query=current_query, limit=max_nodes)
        return self._format_summary(ranked, max_chars=max_chars)

    def build_phase_summary(
        self,
        current_query: Optional[str] = None,
        current_phase: Optional[str] = None,
        current_intent: Optional[str] = None,
        max_nodes: int = 8,
        recent_failures: Optional[List[str]] = None,
        max_chars: int = 2200,
    ) -> str:
        if not self._entries:
            return ""

        # Phase bias: prefer entries tagged with phase-relevant keywords
        phase_boost = 0.0
        phase_filter = None
        if current_phase:
            phase_map = {
                "analyze": ["goal", "constraint", "preference"],
                "locate": ["goal", "relationship", "workflow"],
                "edit": ["decision", "project_state", "workflow"],
                "debug": ["risk", "workflow", "decision"],
                "test": ["risk", "project_state"],
                "reflect": ["lesson", "risk", "decision"],
                "maintenance": ["workflow", "project_state", "lesson"],
            }
            phase_filter = phase_map.get(current_phase.lower())

        ranked = self._rank(
            self._entries,
            query=current_query,
            limit=max_nodes,
            phase_filter=phase_filter,
            recent_failures=recent_failures,
            current_intent=current_intent,
        )
        return self._format_summary(ranked, max_chars=max_chars)

    def get_activation_set(
        self,
        current_query: Optional[str] = None,
        current_phase: Optional[str] = None,
        current_intent: Optional[str] = None,
        limit: int = 5,
        recent_failures: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        if not self._entries:
            return []

        ranked = self._rank(
            self._entries,
            query=current_query,
            limit=limit,
            recent_failures=recent_failures,
            current_intent=current_intent,
        )
        result = []
        for entry in ranked[:limit]:
            result.append({
                "node_id": entry.get("_id", ""),
                "summary": entry.get("summary", "")[:120],
                "cognitive_type": entry.get("cognitive_type", "memory"),
                "score": round(entry.get("_score", 0.0), 3),
                "tags": entry.get("tags", [])[:4],
                "activation_keywords": entry.get("activation_keywords", [])[:6],
            })

        self._last_activation_set = result
        return result

    def evaluate_activation_usage(self, response_text: str) -> Dict[str, Any]:
        if not self._last_activation_set or not response_text:
            return {"referenced_count": 0, "total_count": 0, "usage_ratio": 0.0}

        lower = response_text.lower()
        referenced = 0
        for node in self._last_activation_set:
            summary = str(node.get("summary", "")).lower()[:30]
            keywords = [str(k).lower() for k in node.get("activation_keywords", [])]
            if any(kw and len(kw) >= 2 and kw in lower for kw in keywords + [summary]):
                referenced += 1

        total = len(self._last_activation_set)
        ratio = referenced / total if total > 0 else 0.0
        self._activation_usage_history.append({"referenced": referenced, "total": total, "ratio": ratio})
        return {"referenced_count": referenced, "total_count": total, "usage_ratio": round(ratio, 3)}

    def get_activation_usage_stats(self) -> Dict[str, Any]:
        if not self._activation_usage_history:
            return {"avg_ratio": 0.0, "consecutive_unused": 0, "sample_count": 0}
        ratios = [h["ratio"] for h in self._activation_usage_history]
        avg = sum(ratios) / len(ratios)
        consecutive = 0
        for h in reversed(self._activation_usage_history):
            if h["ratio"] == 0.0:
                consecutive += 1
            else:
                break
        return {"avg_ratio": round(avg, 3), "consecutive_unused": consecutive, "sample_count": len(ratios)}

    def get_adaptive_activation_limit(self, default_limit: int = 5) -> int:
        stats = self.get_activation_usage_stats()
        consecutive = stats.get("consecutive_unused", 0)
        avg = stats.get("avg_ratio", 0.0)
        if consecutive >= 3:
            return min(2, default_limit)
        if consecutive >= 2:
            return min(3, default_limit)
        if avg > 0.4 and stats.get("sample_count", 0) >= 3:
            return min(default_limit + 1, 8)
        return default_limit

    # ── internal: index builder ────────────────────────────────

    def _build_index(self) -> None:
        """Scan sources and rebuild scored entries."""
        entries: List[Dict[str, Any]] = []

        # 1. memory_Write
        if self._write_dir.exists():
            for f in sorted(self._write_dir.iterdir()):
                if not f.is_file() or not f.name.endswith(".txt"):
                    continue
                if _has_exclude_pattern(f.name):
                    continue
                entry = self._parse_write_file(f)
                if entry:
                    entries.append(entry)

        # 2. .agent_history — only the 5 most recent files (keeps it light)
        if self._hist_dir.exists():
            hist_files = sorted(self._hist_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
            for f in hist_files[:5]:
                if not f.is_file() or not f.name.endswith(".json"):
                    continue
                entries.extend(self._parse_history_file(f))

        # Deduplicate by content hash
        seen = set()
        deduped = []
        for e in entries:
            key = e.get("summary", "")[:120]
            if key and key not in seen:
                seen.add(key)
                deduped.append(e)

        # Score and forget
        deduped = [self._score_entry(e) for e in deduped]
        deduped = [e for e in deduped if self._keep_entry(e)]
        deduped.sort(key=lambda e: e.get("_score", 0.0), reverse=True)

        self._entries = deduped

        # Persist cache
        self._save_cache()

    def _parse_write_file(self, path: Path) -> Optional[Dict[str, Any]]:
        """Parse a memory_Write .txt file into a scored entry."""
        try:
            content = path.read_text(encoding="utf-8").strip()
        except Exception:
            return None
        if not content or len(content) < 20:
            return None

        fname = path.name
        created = _filename_date(fname) or datetime.fromtimestamp(path.stat().st_mtime)
        mtime = path.stat().st_mtime

        # Extract title from filename (after timestamp)
        title = re.sub(r"^\d{4}[-_]\d{2}[-_]\d{2}[-_]\d{2}[-_]\d{2}[-_]\d{2}_?", "", fname)
        title = title.replace(".txt", "").replace("_", " ").strip()

        # Score importance from title + content keywords
        importance = self._calc_importance(title, content)

        # Tags from filename
        tags = []
        for kw in ["用户", "关系", "伙伴", "架构", "规划", "目标", "总结", "指南", "经验", "教训", "风险", "身份"]:
            if kw in title:
                tags.append(kw)

        # Determine cognitive_type from title
        ctype = self._infer_type(title, content)

        return {
            "_id": fname,
            "_mtime": mtime,
            "_created": created.isoformat() if hasattr(created, "isoformat") else str(created),
            "_days_old": _days_since(created) or 999.0,
            "summary": content[:300],
            "cognitive_type": ctype,
            "importance": importance,
            "tags": tags,
            "activation_keywords": self._extract_keywords(title, content),
            "title": title,
        }

    def _parse_history_file(self, path: Path) -> List[Dict[str, Any]]:
        """Parse a .agent_history JSON and extract user topics/decisions."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        msgs = data.get("messages", [])
        if not msgs:
            return []

        updated = data.get("updated_at", "")
        mtime = path.stat().st_mtime

        entries = []
        # Extract user messages as memory seeds
        user_msgs = [m.get("content", "") for m in msgs if m.get("role") == "user" and m.get("content")]
        # Take first and last meaningful user message
        meaningful = [m for m in user_msgs if len(str(m).strip()) > 10]
        if not meaningful:
            return []

        # Build one summary per session
        first = str(meaningful[0])[:200]
        last = str(meaningful[-1])[:200]

        # Extract any tool calls/decisions from the session
        decisions = []
        for m in msgs:
            if m.get("role") == "assistant" and "name" in str(m.get("content", "")):
                content = str(m.get("content", ""))
                # Look for tool call indicators
                for keyword in ["决定", "使用", "load_module", "执行", "已完成"]:
                    if keyword in content:
                        decisions.append(content[:120])
                        break

        session_id = data.get("session_id", path.stem)
        summary_text = f"会话 {session_id}: {first[:80]}"
        if decisions:
            summary_text += f" | 工具决策: {'; '.join(decisions[:3])}"

        entries.append({
            "_id": path.name,
            "_mtime": mtime,
            "_created": updated or datetime.fromtimestamp(mtime).isoformat(),
            "_days_old": 0.0,  # fresh
            "summary": summary_text[:300],
            "cognitive_type": "history",
            "importance": 0.5,  # history is moderate importance
            "tags": ["history"],
            "activation_keywords": self._tokenize(first)[:6],
            "title": f"会话 {session_id}",
        })

        return entries

    # ── scoring ─────────────────────────────────────────────────

    @staticmethod
    def _calc_importance(title: str, content: str) -> float:
        score = 1.0
        combined = (title + " " + content).lower()

        # Title keyword boost
        for kw, boost in IMPORTANCE_KEYWORDS.items():
            if kw.lower() in combined:
                score += boost

        # Content depth (longer = more substantial)
        lines = content.split("\n")
        if len(lines) > 20:
            score += 1.0
        elif len(lines) > 10:
            score += 0.5

        # Has structured content
        if re.search(r"[#*-]\s", content):
            score += 0.5
        if "##" in content:
            score += 0.3

        # Penalty for tool-noise content (even if filename passed filter)
        for word in TOOL_NOISE_WORDS:
            if word in combined:
                score -= 2.0

        return max(0.1, min(10.0, score))

    @staticmethod
    def _infer_type(title: str, content: str) -> str:
        combined = (title + " " + content[:200]).lower()
        if any(w in combined for w in ["目标", "goal", "规划"]):
            return "goal"
        if any(w in combined for w in ["限制", "约束", "constraint"]):
            return "constraint"
        if any(w in combined for w in ["决策", "决定", "选择"]):
            return "decision"
        if any(w in combined for w in ["偏好", "preference", "喜欢"]):
            return "preference"
        if any(w in combined for w in ["状态", "进展", "当前"]):
            return "project_state"
        if any(w in combined for w in ["风险", "危险", "注意"]):
            return "risk"
        if any(w in combined for w in ["教训", "经验", "学到", "lesson"]):
            return "lesson"
        if any(w in combined for w in ["关系", "伙伴", "relationship"]):
            return "relationship"
        if any(w in combined for w in ["身份", "自我", "我是", "identity"]):
            return "identity"
        if any(w in combined for w in ["工作流", "流程", "workflow"]):
            return "workflow"
        return "memory"

    @staticmethod
    def _extract_keywords(title: str, content: str) -> List[str]:
        text = (title + " " + content[:500]).lower()
        words = re.findall(r"[a-z0-9_]{3,}|[\u4e00-\u9fff]{2,6}", text)
        # Count and return top keywords
        counter = Counter(w for w in words if w not in TOOL_NOISE_WORDS and len(w) >= 2)
        return [w for w, _ in counter.most_common(8)]

    def _score_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        importance = entry.get("importance", 1.0)
        days_old = entry.get("_days_old", 999.0)

        # Recency boost
        if days_old < 1:
            recency = 3.0
        elif days_old < 7:
            recency = 2.5
        elif days_old < 30:
            recency = 1.5
        elif days_old < 90:
            recency = 0.5
        else:
            recency = 0.0

        score = importance * 1.5 + recency * 1.2
        # Type bonus
        type_bonus = {
            "identity": 2.0, "goal": 1.8, "constraint": 1.6,
            "decision": 1.5, "relationship": 1.4, "risk": 1.3,
            "lesson": 1.2, "project_state": 1.0, "workflow": 0.6,
            "memory": 0.8, "history": 0.5,
        }.get(entry.get("cognitive_type", "memory"), 0.5)
        score += type_bonus

        entry["_score"] = score
        entry["_recency"] = round(recency, 1)
        return entry

    def _keep_entry(self, entry: Dict[str, Any]) -> bool:
        """Forgetting policy: remove old/low entries."""
        days_old = entry.get("_days_old", 999.0)
        importance = entry.get("importance", 0.0)
        score = entry.get("_score", 0.0)

        # Always keep high-importance memory regardless of age
        if importance >= 6.0:
            return True

        # Old + low importance = forget
        if days_old > 180 and importance < 3.0:
            return False
        if days_old > 90 and importance < 2.0:
            return False
        if days_old > 365:
            return False

        # Very low score = forget
        if score < 1.0:
            return False

        return True

    # ── ranking ─────────────────────────────────────────────────

    def _rank(
        self,
        entries: List[Dict[str, Any]],
        query: Optional[str] = None,
        limit: int = 10,
        phase_filter: Optional[List[str]] = None,
        recent_failures: Optional[List[str]] = None,
        current_intent: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query_terms = self._tokenize(query or "")
        failure_terms = self._tokenize(" ".join(recent_failures or []))
        intent_terms = self._tokenize(current_intent or "")

        scored = []
        for e in entries:
            score = e.get("_score", 0.0)

            # Query match boost
            searchable = (e.get("summary", "") + " " + e.get("title", "")).lower()
            query_matches = sum(1 for t in query_terms if t in searchable)
            score += query_matches * 2.0

            # Failure match boost
            fail_matches = sum(1 for t in failure_terms if t in searchable)
            score += fail_matches * 1.5

            # Intent match
            intent_matches = sum(1 for t in intent_terms if t in searchable)
            score += intent_matches * 1.0

            # Phase filter (filter out non-matching types if phase is specific)
            if phase_filter:
                ctype = e.get("cognitive_type", "")
                # Phase boost: up to +2.0 for matching
                if ctype in phase_filter:
                    score += 2.0

            # Add some diversity: penalize duplicate cognitive_types
            entry_copy = dict(e)
            entry_copy["_score"] = score
            scored.append(entry_copy)

        scored.sort(key=lambda x: x["_score"], reverse=True)

        # Diversity: limit same type in top results
        result = []
        type_count: Counter = Counter()
        for e in scored:
            ctype = e.get("cognitive_type", "memory")
            if type_count[ctype] >= min(3, max(1, limit // 3)):
                continue
            result.append(e)
            type_count[ctype] += 1
            if len(result) >= limit:
                break

        return result

    # ── output formatting ──────────────────────────────────────

    def _format_summary(self, ranked: List[Dict[str, Any]], max_chars: int = 2600) -> str:
        if not ranked:
            return ""

        lines = ["# Cognitive State"]
        # Group by type
        grouped: Dict[str, List[str]] = {}
        for e in ranked:
            ctype = e.get("cognitive_type", "memory")
            grouped.setdefault(ctype, []).append(e)

        type_heading = {
            "identity": "Identity", "goal": "Goals", "constraint": "Constraints",
            "decision": "Decisions", "preference": "Preferences",
            "project_state": "Current State", "risk": "Risks",
            "lesson": "Lessons", "relationship": "Relationships",
            "workflow": "Workflows", "memory": "Memories", "history": "Session History",
        }

        for ctype in ["identity", "goal", "constraint", "decision", "relationship",
                       "lesson", "risk", "project_state", "workflow", "preference",
                       "memory", "history"]:
            nodes = grouped.get(ctype, [])
            if not nodes:
                continue
            heading = type_heading.get(ctype, ctype.capitalize())
            lines.append(f"\n## {heading}")
            for node in nodes[:3]:
                summary = str(node.get("summary", "") or "")[:120]
                if not summary:
                    continue
                line = f"- {summary}"
                tags = node.get("tags", [])
                if tags:
                    line += f" ({', '.join(tags[:3])})"
                lines.append(line)

        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[: max_chars - 3].rstrip() + "..."
        return text

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        if not text:
            return []
        lower = text.lower()
        tokens = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{1,8}", lower)
        seen = set()
        deduped = []
        for t in tokens:
            if t not in seen:
                deduped.append(t)
                seen.add(t)
        return deduped

    # ── cache persistence ───────────────────────────────────────

    def _save_cache(self) -> None:
        """Persist scored entries to cache file for faster startup."""
        try:
            payload = {
                "version": 2,
                "updated_at": _now().isoformat(),
                "entries": self._entries,
            }
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass


# Compatibility shorthand
CognitiveNetworkState.__module__ = __name__
