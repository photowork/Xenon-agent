#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
技能指引管理器
将成功的任务步骤总结为可复用的系列步骤指引，保存在 Memory/skill 文件夹中，
支持关键词搜索、文件列表浏览和内容读取。
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


class SkillToolManager:
    """技能指引工具管理器 — 保存/搜索/读取/管理可复用的任务步骤指引"""

    def __init__(self, skill_dir: Optional[str] = None):
        if skill_dir is None:
            self.skill_dir = Path(__file__).parent.parent / "Memory" / "skill"
        else:
            self.skill_dir = Path(skill_dir)
        self.skill_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.skill_dir / "_index.json"
        self._ensure_index()

    # ------------------------------------------------------------------ #
    #  内部工具方法
    # ------------------------------------------------------------------ #

    def _ensure_index(self) -> None:
        """确保索引文件存在"""
        if not self.index_path.exists():
            self._save_index({})

    def _load_index(self) -> Dict[str, Any]:
        """加载索引文件"""
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _save_index(self, index: Dict[str, Any]) -> None:
        """保存索引文件"""
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

    def _sanitize_filename(self, name: str) -> str:
        """将名称转换为安全的文件名"""
        safe = re.sub(r'[\\/:*?"<>|]+', "_", name)
        safe = re.sub(r"\s+", "_", safe)
        return safe.strip("_")[:80]

    def _make_filename(self, name: str, suffix: str = "") -> str:
        """生成带时间戳的文件名"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = self._sanitize_filename(name)
        if suffix:
            return f"{ts}_{base}_{suffix}.md"
        return f"{ts}_{base}.md"

    def _entry_from_file(self, file_path: Path) -> Optional[Dict[str, Any]]:
        """从 markdown 文件元数据构建索引条目"""
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception:
            return None

        entry: Dict[str, Any] = {
            "filename": file_path.name,
            "path": str(file_path),
            "title": file_path.stem,
            "tags": [],
            "category": "",
            "summary": "",
            "created": datetime.fromtimestamp(file_path.stat().st_mtime).isoformat(),
            "size": file_path.stat().st_size,
        }

        # 尝试解析 YAML front matter
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                front_matter = parts[1]
                for line in front_matter.strip().split("\n"):
                    line = line.strip()
                    if ":" in line:
                        key, _, value = line.partition(":")
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key == "title":
                            entry["title"] = value
                        elif key == "tags":
                            entry["tags"] = [t.strip() for t in value.strip("[]").split(",") if t.strip()]
                        elif key == "category":
                            entry["category"] = value
                        elif key == "summary":
                            entry["summary"] = value
            entry["content"] = parts[2].strip() if len(parts) >= 3 else content.strip()
        else:
            entry["content"] = content.strip()

        # 无 front matter 时用首行作为 title
        if entry["title"] == file_path.stem:
            first_line = content.strip().split("\n")[0].lstrip("#").strip()
            if first_line:
                entry["title"] = first_line

        # 摘要：用 front matter 中的 summary，或取正文前 200 字
        if not entry["summary"]:
            body = entry.get("content", "")
            entry["summary"] = body[:200]

        return entry

    # ------------------------------------------------------------------ #
    #  公开工具方法
    # ------------------------------------------------------------------ #

    def save_skill(
        self,
        name: str,
        description: str,
        steps: list,
        tags: list = None,
        category: str = "",
    ) -> Dict[str, Any]:
        """
        将成功任务步骤总结保存为技能指引文件（保存到 Memory/skill 目录）

        :param name: 技能/任务名称（简短标题）
        :param description: 任务整体描述（目标、背景、注意事项）
        :param steps: 步骤列表，每项为 {"step": 序号, "action": 操作, "detail": 详细说明, "tip": 提示}
        :param tags: 标签列表，用于分类搜索，如 ["python", "debug"]
        :param category: 分类名，如 "编程", "系统管理"
        :return: 保存结果，含 filename 和内容预览
        """
        try:
            tags = tags or []
            filename = self._make_filename(name)

            # 构建 YAML front matter
            front_matter_lines = [
                "---",
                f'title: "{name}"',
            ]
            if tags:
                front_matter_lines.append(f"tags: [{', '.join(tags)}]")
            if category:
                front_matter_lines.append(f'category: "{category}"')
            # 简短摘要
            summary_short = description[:120].replace("\n", " ")
            front_matter_lines.append(f'summary: "{summary_short}"')
            front_matter_lines.extend([
                f"created: {datetime.now().isoformat()}",
                "---",
                "",
            ])

            # 构建正文
            body_lines = [
                f"# {name}",
                "",
                "## 概述",
                "",
                description.strip(),
                "",
                "## 步骤指引",
                "",
            ]

            if not steps:
                body_lines.append("（暂无步骤）")
            else:
                for i, s in enumerate(steps, 1):
                    if isinstance(s, dict):
                        step_num = s.get("step", i)
                        action = s.get("action", "")
                        detail = s.get("detail", "")
                        tip = s.get("tip", "")
                    else:
                        step_num = i
                        action = str(s)
                        detail = ""
                        tip = ""

                    body_lines.append(f"### 步骤 {step_num}：{action}")
                    if detail:
                        body_lines.append("")
                        body_lines.append(detail)
                    if tip:
                        body_lines.append("")
                        body_lines.append(f"> 💡 提示：{tip}")
                    body_lines.append("")

            body_lines.extend([
                "---",
                "",
                f"*本指引由 Xenon 于 {datetime.now().strftime('%Y-%m-%d %H:%M')} 生成*",
            ])

            full_content = "\n".join(front_matter_lines) + "\n".join(body_lines)
            file_path = self.skill_dir / filename
            file_path.write_text(full_content, encoding="utf-8")

            # 更新索引
            index = self._load_index()
            index[filename] = {
                "title": name,
                "tags": tags,
                "category": category,
                "summary": summary_short,
                "created": datetime.now().isoformat(),
                "steps_count": len(steps),
            }
            self._save_index(index)

            return {
                "success": True,
                "filename": filename,
                "path": str(file_path),
                "preview": full_content[:500],
                "message": f"技能指引已保存: {filename}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def search_skills(
        self,
        keyword: str,
        limit: int = 10,
        search_content: bool = True,
    ) -> Dict[str, Any]:
        """
        通过关键词搜索技能指引文件（同时搜索文件名、标签和文件内容）

        :param keyword: 搜索关键词
        :param limit: 返回结果数量上限
        :param search_content: 是否同时搜索文件内容（默认 True）
        :return: 匹配结果列表，含文件名、标题、标签、摘要、匹配片段
        """
        try:
            keyword_lower = keyword.lower()
            results: List[Dict[str, Any]] = []

            for file_path in sorted(self.skill_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
                entry = self._entry_from_file(file_path)
                if entry is None:
                    continue

                score = 0
                match_context = ""

                # 名称匹配
                if keyword_lower in entry["title"].lower():
                    score += 10
                if keyword_lower in entry.get("filename", "").lower():
                    score += 5
                # 标签匹配
                for tag in entry.get("tags", []):
                    if keyword_lower in tag.lower():
                        score += 8
                        break
                # 摘要匹配
                if keyword_lower in entry.get("summary", "").lower():
                    score += 3

                # 内容搜索
                if search_content and score < 5:
                    body = entry.get("content", "")
                    if keyword_lower in body.lower():
                        score += 2
                        # 提取匹配片段
                        idx = body.lower().find(keyword_lower)
                        start = max(0, idx - 60)
                        end = min(len(body), idx + len(keyword) + 60)
                        match_context = "..." + body[start:end] + "..."

                if score > 0:
                    results.append({
                        "filename": entry["filename"],
                        "title": entry["title"],
                        "tags": entry.get("tags", []),
                        "category": entry.get("category", ""),
                        "summary": entry.get("summary", "")[:150],
                        "score": score,
                        "match_context": match_context,
                        "created": entry.get("created", ""),
                        "size": entry.get("size", 0),
                    })

            results.sort(key=lambda r: r["score"], reverse=True)
            results = results[:limit]

            return {
                "success": True,
                "keyword": keyword,
                "total_matches": len(results),
                "results": results,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def list_skills(
        self,
        page: int = 1,
        page_size: int = 20,
        category: str = "",
        tag: str = "",
    ) -> Dict[str, Any]:
        """
        列出所有技能指引文件（支持分页和按分类/标签筛选）

        :param page: 页码，从 1 开始
        :param page_size: 每页数量
        :param category: 按分类筛选（留空则显示全部）
        :param tag: 按标签筛选（留空则显示全部）
        :return: 文件列表，含分页信息
        """
        try:
            all_entries: List[Dict[str, Any]] = []
            for file_path in sorted(self.skill_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
                entry = self._entry_from_file(file_path)
                if entry is None:
                    continue

                # 筛选
                if category and entry.get("category", "").lower() != category.lower():
                    continue
                if tag and tag.lower() not in [t.lower() for t in entry.get("tags", [])]:
                    continue

                all_entries.append({
                    "filename": entry["filename"],
                    "title": entry["title"],
                    "tags": entry.get("tags", []),
                    "category": entry.get("category", ""),
                    "summary": entry.get("summary", "")[:150],
                    "created": entry.get("created", ""),
                    "size": entry.get("size", 0),
                })

            total = len(all_entries)
            total_pages = max(1, (total + page_size - 1) // page_size)
            start = (page - 1) * page_size
            end = start + page_size
            page_items = all_entries[start:end]

            return {
                "success": True,
                "pagination": {
                    "current_page": page,
                    "page_size": page_size,
                    "total_items": total,
                    "total_pages": total_pages,
                    "has_more": page < total_pages,
                },
                "items": page_items,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def read_skill(self, filename: str) -> Dict[str, Any]:
        """
        读取指定技能指引文件的完整内容

        :param filename: 技能文件名（如 "20260601_120000_示例技能.md"）
        :return: 文件完整元数据和内容
        """
        try:
            file_path = self.skill_dir / filename
            if not file_path.exists():
                # 尝试模糊匹配
                matches = list(self.skill_dir.glob(f"*{filename}*"))
                if not matches:
                    return {
                        "success": False,
                        "error": f"未找到文件: {filename}",
                    }
                file_path = matches[0]

            entry = self._entry_from_file(file_path)
            if entry is None:
                return {"success": False, "error": f"无法读取文件: {filename}"}

            return {
                "success": True,
                "filename": file_path.name,
                "title": entry["title"],
                "tags": entry.get("tags", []),
                "category": entry.get("category", ""),
                "summary": entry.get("summary", ""),
                "created": entry.get("created", ""),
                "size": entry.get("size", 0),
                "content": entry.get("content", ""),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_skill(self, filename: str) -> Dict[str, Any]:
        """
        删除指定的技能指引文件

        :param filename: 技能文件名
        :return: 删除结果
        """
        try:
            file_path = self.skill_dir / filename
            if not file_path.exists():
                return {"success": False, "error": f"文件不存在: {filename}"}

            file_path.unlink()

            # 更新索引
            index = self._load_index()
            index.pop(filename, None)
            self._save_index(index)

            return {
                "success": True,
                "filename": filename,
                "message": f"已删除: {filename}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def update_skill(
        self,
        filename: str,
        content: str = "",
        tags: list = None,
        category: str = "",
    ) -> Dict[str, Any]:
        """
        更新已有技能指引的元数据或内容

        :param filename: 技能文件名
        :param content: 新的正文内容（留空则不修改）
        :param tags: 新的标签列表（留空则不修改）
        :param category: 新的分类（留空则不修改）
        :return: 更新结果
        """
        try:
            file_path = self.skill_dir / filename
            if not file_path.exists():
                return {"success": False, "error": f"文件不存在: {filename}"}

            entry = self._entry_from_file(file_path)
            if entry is None:
                return {"success": False, "error": f"无法读取文件: {filename}"}

            new_tags = tags if tags is not None else entry.get("tags", [])
            new_category = category if category else entry.get("category", "")
            new_summary = entry.get("summary", "")

            # 构建新的 front matter
            fm_lines = [
                "---",
                f'title: "{entry["title"]}"',
            ]
            if new_tags:
                fm_lines.append(f"tags: [{', '.join(new_tags)}]")
            if new_category:
                fm_lines.append(f'category: "{new_category}"')
            fm_lines.append(f'summary: "{new_summary}"')
            fm_lines.append(f"created: {entry.get('created', datetime.now().isoformat())}")
            fm_lines.append(f"updated: {datetime.now().isoformat()}")
            fm_lines.extend(["---", ""])

            new_content = content if content else entry.get("content", "")
            full = "\n".join(fm_lines) + "\n" + new_content

            file_path.write_text(full, encoding="utf-8")

            # 更新索引
            index = self._load_index()
            index[filename] = {
                "title": entry["title"],
                "tags": new_tags,
                "category": new_category,
                "summary": new_summary,
                "created": entry.get("created", ""),
                "updated": datetime.now().isoformat(),
            }
            self._save_index(index)

            return {
                "success": True,
                "filename": filename,
                "message": f"已更新: {filename}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_categories(self) -> Dict[str, Any]:
        """
        获取所有技能指引的分类统计

        :return: 分类列表及每个分类下的文件数量
        """
        try:
            cat_count: Dict[str, int] = {}
            for file_path in self.skill_dir.glob("*.md"):
                entry = self._entry_from_file(file_path)
                if entry is None:
                    continue
                cat = entry.get("category", "未分类") or "未分类"
                cat_count[cat] = cat_count.get(cat, 0) + 1

            return {
                "success": True,
                "categories": [
                    {"name": k, "count": v}
                    for k, v in sorted(cat_count.items(), key=lambda x: -x[1])
                ],
                "total_files": sum(cat_count.values()),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
